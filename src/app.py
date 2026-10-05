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
import _tkinter
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from combobox import SearchableCombobox
from constants import (C, VERSION, SEARCH_MODES, SEARCH_MODE_GROUPS, _EXT_MAP, WAL_STATES,
                       search_mode_key,
                       mode_label, source_name, source_key, wal_state_label)
from utils import (_q, fmtb, vb, _snippet, fmt_count, _int_count, plural, longest,
                   blob_type, _build_schema_text,
                   _build_schema_html, set_write_guard, write_allowed, safe_filename,
                   export_row_blobs, flag_summary, plain_text, json_value, sql_first_keyword,
                   sql_reads_only)
from database import DB
from case import Case
from case_ui import OpenFolderDialog
from navigator import CaseNavigator, case_name, member_warnings, short_count
from overview_tab import OverviewTab
from palette import CommandPalette, Item
from parts import Chip, StatusLine, Toolbar
from scope import ScopePicker, Scopes, scope_text
from tokens import COLOR as K, FONT as F, XS, S, M
from engine.tags import (TagError, add_recent_case, case_changes, case_file_path, read_case,
                         sha256_change, write_case)
from widgets import (ElideLabel, FlowFrame, SearchBox, TextFind, ToolTip, TreeFilter,
                     TreeviewTooltip, add_placeholder, cancel_all_afters, fit_geometry,
                     focus_search_in,
                     menu_button, place_over, setup_theme, wrap_to_width)
from dialogs import HelpDialog, ScopeDlg, BlobViewer, RowWin, TextWindow, ValuesWindow
from search_results import ResultGrouper, is_wal, match_label, source_kind
from forensics_tab import ForensicsTab, RecordWindow
from timeline_tab import TimelineTab
from date_columns import BrowseDates
from lookups import BrowseLookups
from engine import limits, uiyield
from engine.decode import timestamps
from engine.evidence import is_network_path
from engine.export import mtime_utc as mtime_text, utc_text
from engine.search import check_term
from grid import DataGrid, Runner, grid_search, show_filter_help, warm_fallback_fonts
from jobs import (Job, ask_path, blob_export_done, evidence_records, export_options,
                  export_protected, export_rows, multi_table_options, write_export_manifest)
from browse_sources import ListSource, TableSource
from tagging import Tagging
from wal_tab import WalTab, open_wal_record
from tags_tab import TagsTab
from relations_view import RelationWindows
from relations_tab import RelationsTab
from datamap_ui import DataMapUI
from engine.relations import ROWID
from engine.filters import value_expr
from engine.schema import Locator


# Tk events served while _stop_workers waits: the calls worker threads make into Tk (after(),
# variable reads) are always served; FILE_EVENTS only keeps Tcl from treating an empty event mask
# as "all events", so user input and timers stay queued until the wait is over.
WORKER_CALLS_ONLY = _tkinter.DONT_WAIT | _tkinter.FILE_EVENTS
WORKER_STOP_WAIT = 3.0      # seconds _close_db waits for the worker threads to end
UI_BEAT_MS = 10             # the Tk thread tells the workers it serves events (uiyield)
NAV_BREAKPOINT = 1000       # a window narrower than this folds the navigator to its toggle

# Interface-size choices (View menu): key, menu label, multiplier of the platform's
# own Tk scaling. Point-sized fonts follow "tk scaling", so the whole UI grows/shrinks.
UI_SCALES = (("compact", "Small", 0.9),
             ("default", "Default", 1.0),
             ("large", "Large", 1.2),
             ("xlarge", "Extra large", 1.4))
_UI_SCALE_FACTORS = dict((k, f) for k, _label, f in UI_SCALES)


# ── Combobox type-ahead helper ────────────────────────────────────────────
# ── Main Application ─────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self, initial_path=None):
        from widgets import enable_dpi_awareness
        enable_dpi_awareness()          # crisp at 125-200% display scaling
        super().__init__()
        forced = os.environ.get("SGA_TK_SCALING")      # tests: a display scaling to lay out at
        if forced:
            try:
                self.tk.call("tk", "scaling", float(forced))
            except (tk.TclError, ValueError):
                pass
        try:
            self._base_scaling = float(self.tk.call("tk", "scaling"))
        except (tk.TclError, ValueError):
            self._base_scaling = 96 / 72.0
        self.title("SQLite GUI Analyzer v" + VERSION)
        # sizes grow with the display scaling (Tk's scaling is 1.33 at 100%)
        try:
            f = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72.0))
        except (tk.TclError, ValueError):
            f = 1.0
        self.geometry("%dx%d" % (1200 * f, 750 * f))
        self.configure(bg=C["bg"])
        # never larger than the screen (a 1080p screen at 200%: 1800x1000 only just fitted)
        self.minsize(min(int(900 * f), max(600, self.winfo_screenwidth() - 40)),
                     min(int(500 * f), max(400, self.winfo_screenheight() - 100)))
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
        # The open databases: a case of one or more. self.db is the active one's DB (see the
        # property), so every tab written for one database works on it unchanged.
        self.case = Case()
        self._no_db = DB()
        self._no_counts, self._no_scope = {}, []
        self._case_path = None           # the saved case file (2+ databases)
        self._case_state = {}            # what the case file keeps besides the databases
        self._search_cancel = False
        self._safe_paths = set()     # databases the user chose to open with Safe parse
        self._search_thread = None
        self._search_threads = []    # threads searching the databases of a case side by side
        self._search_gen = 0         # bumped by each search
        self._count_gen = 0          # bumped on every open/close; stale count threads stop
        self._bg_count_thread = None
        self._count_threads = []
        self._count_queue = []           # databases waiting for the row-count worker
        self._count_lock = threading.Lock()
        self._count_worker, self._count_worker_gen = None, None
        self._browse_source = None       # the Browse grid's row source (browse_sources)
        self._browse_count_gen = 0       # bumped per count request; stale counts are dropped
        self._browse_count_error = ""
        self._browse_pos_gen = 0         # bumped per position index request (engine.positions)
        self._browse_pos_busy = False    # a position index is being built for the Browse view
        self._browse_pos_db = None       # the database (case member) it is built on
        self._browse_t0 = None           # when the table was chosen: first-rows time
        self._browse_first_ms = None
        self._browse_first_range = None  # the rows that time was measured for
        self._browse_table_gen = 0       # bumped per table choice; stale table reads are dropped
        self._browse_has_blobs = False   # the Browse table holds BLOB values (checked on a worker)
        self._browse_wal_loading = None  # 'WAL: name' whose records are being read
        self._jobs = []                  # jobs.Job running now (exports, verification...)
        self._open_state = None          # the databases being opened (_start_open)
        self._open_waiting = []          # opens asked for while one runs
        self._open_finishing = None      # an open whose databases are being shown everywhere
        self._case_change_gen = 0        # bumped per _case_changed (a spread one stops)
        self._close_verify = None        # the evidence of databases just closed, verifying
        self.last_closed = []            # what the last close verified, database by database
        self._activity_log = None        # engine.activity.ActivityLog of the open case
        self._schema_filter_after = None
        self._load_time = 0
        self._measure_font = None

        # SQL Query Editor state
        self._sql_query_history     = []
        self._sql_query_history_idx = -1
        self._sql_query_thread      = None
        self._sql_query_cancel      = False
        self._sql_conn              = None   # connection of the running SQL-tab query
        self._sql_result_rows       = []
        self._sql_result_cols       = []
        self._sql_limited           = False  # the last query stopped at the row limit

        set_write_guard(lambda p: self.case.is_protected(p))
        # Row tags of the open database (saved in the user's app-data folder, never beside it)
        self.tags = Tagging(self)
        from engine.tags import data_dir
        self._data_dir_in_use = data_dir()   # a changed folder is used from the next start
        self._theme_var = tk.StringVar(self, value="light")
        self.set_theme(self.tags.settings.get("theme") or "light", save=False)
        self._ui_scale_var = tk.StringVar(self, value="default")
        self.set_ui_scale(self.tags.settings.get("ui_scale") or "default", save=False)
        # Column relationship windows (related rows of a value, the map of a column)
        self.relations = RelationWindows(self)
        # Copy with related and the Database Map (datamap_ui)
        self.datamap = DataMapUI(self)
        self._browse_dates = BrowseDates(self)      # 'Show as date' of the Browse columns
        self._browse_lookups = BrowseLookups(self)  # 'Show value from linked table'
        # which databases each feature covers (one scope control everywhere)
        self.scopes = Scopes(lambda: list(self.case))
        self.scopes.listeners.append(lambda features: self._save_case())
        self._palette = None
        self._build_header()
        self._build_body()
        self._bind_keys()
        self._setup_tooltips()
        if self.tags.settings.get("navigator_hidden") is True:
            self._set_sidebar(False)    # hidden by hand last time: still hidden
        self._update_welcome(False)     # nothing is open yet
        self._tags_tab = TagsTab(self._nb, self)
        self._nb.add(self._tags_tab, text="Tagged")
        self._relations_tab = RelationsTab(self._nb, self)
        self._nb.add(self._relations_tab, text="Relationships")
        # the landing page of a case of several databases (added only for one)
        self._overview = OverviewTab(self._nb, self)
        self._overview_added = False
        self.tags.bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)
        # a narrow window gives the schema sidebar less room, the tabs more
        self.bind("<Configure>", self._on_resize, add="+")
        self._issues_win = None
        self._verify_close_win = None    # the window of the re-hash on close, while it runs
        self._poll_issue_count()
        self._ui_beat()
        self._warm_tabs()

        if initial_path and os.path.isfile(initial_path):
            self.after(200, lambda: self._open_db(initial_path))

    def _storage_text(self):
        """The saved-data folder in use, and the one used from the next start if it was
        changed."""
        from engine.tags import data_dir
        now = self._data_dir_in_use
        nxt = data_dir()
        if os.path.normcase(nxt) == os.path.normcase(now):
            return now
        return "%s\nfrom the next start: %s" % (now, nxt)

    def _refresh_welcome_storage(self):
        lbl = getattr(self, "_welcome_store", None)
        if lbl is not None:
            lbl.configure(text=self._storage_text())

    def open_data_folder(self):
        """Open the saved-data folder in the file manager (made first if it is new: it is
        the tool's own folder, never an evidence folder)."""
        import subprocess
        from engine.tags import data_dir
        path = data_dir()
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as e:
            messagebox.showerror("Open folder", str(e), parent=self)

    def _show_storage(self):
        """Where the app keeps its data (tags, recent files, settings): what is stored,
        where, and how to change it."""
        from engine.tags import data_dir, save_settings, DATA_DIR_ENV
        import os
        win = tk.Toplevel(self)
        win.title("Storage")
        win.transient(self)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Saved data folder", style="B.TLabel").pack(anchor="w")
        folder_lbl = ttk.Label(frm, text=self._storage_text(), style="Mono.TLabel", wraplength=420,
                               justify="left")
        folder_lbl.pack(anchor="w", pady=(2, 4))
        fb = ttk.Frame(frm)
        fb.pack(anchor="w")
        ttk.Button(fb, text="Open folder",
                   command=self.open_data_folder).pack(side="left")
        ttk.Button(fb, text="Change folder\u2026",
                   command=lambda: self._change_data_dir(win, folder_lbl)).pack(side="left", padx=6)
        from engine.tags import data_dir_override
        if data_dir_override() or os.environ.get(DATA_DIR_ENV):
            ttk.Button(fb, text="Reset to default",
                       command=lambda: self._reset_data_dir(win, folder_lbl)).pack(side="left")
        ttk.Label(frm, text="Your tags, recent files and settings move with the folder. "
                  "Takes effect after you restart the app.", style="Muted.TLabel", wraplength=420,
                  justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text="What is stored there", style="B.TLabel").pack(anchor="w", pady=(12, 0))
        for what in ("settings.json \u2013 recent databases and cases, theme, window state",
                     "cases/<database>-<id>.json \u2013 your tags and saved view state per database",
                     "case-lists/ \u2013 multi-database case definitions"):
            ttk.Label(frm, text="\u2022 " + what, style="M.TLabel", wraplength=420,
                      justify="left").pack(anchor="w")
        ttk.Label(frm, text="Nothing is ever written into your evidence databases.",
                  style="Muted.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text="Recent items kept", style="B.TLabel").pack(anchor="w", pady=(12, 0))
        nvar = tk.IntVar(value=self.tags.settings.get("recent_max", 10))
        sf = ttk.Frame(frm)
        sf.pack(anchor="w", pady=(2, 0))
        ttk.Spinbox(sf, from_=0, to=50, width=5, textvariable=nvar).pack(side="left")
        ttk.Label(sf, text="databases and cases in Open \u25be Recent",
                  style="M.TLabel").pack(side="left", padx=(6, 0))

        def _save():
            try:
                self.tags.settings["recent_max"] = int(nvar.get())
            except (tk.TclError, ValueError):
                pass
            try:
                save_settings(self.tags.settings)
            except (OSError, ValueError):
                pass
            win.destroy()

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="OK", style="P.TButton", command=_save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())

    def _change_data_dir(self, win, folder_lbl):
        """Pick a new app-data folder: optionally move the current data there, point the
        app at it, and ask for a restart."""
        from tkinter import filedialog, messagebox
        from engine.tags import data_dir, set_data_dir_override
        import os, shutil
        new = filedialog.askdirectory(parent=win, title="Choose the app data folder",
                                      mustexist=False)
        if not new:
            return
        new = os.path.abspath(new)
        old = data_dir()
        if os.path.normcase(new) == os.path.normcase(old):
            return
        move = messagebox.askyesno("Move app data?",
            "Move your tags, recent files and settings to the new folder?\n\n"
            "Yes: everything is copied there now.\n"
            "No: the new folder starts empty (your old data stays where it is).",
            parent=win)
        if move:
            try:
                os.makedirs(new, exist_ok=True)
                for name in ("settings.json", "cases", "case-lists"):
                    src = os.path.join(old, name)
                    dst = os.path.join(new, name)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    elif os.path.isfile(src) and not os.path.exists(dst):
                        shutil.copy2(src, dst)
            except OSError as e:
                messagebox.showerror("Move failed", "Could not copy the data:\n%s" % e,
                                     parent=win)
                return
        try:
            set_data_dir_override(new)
        except OSError as e:
            messagebox.showerror("Not saved", "Could not remember the new folder:\n%s" % e,
                                 parent=win)
            return
        folder_lbl.configure(text=self._storage_text())
        self._refresh_welcome_storage()
        messagebox.showinfo("Restart needed",
            "The new app data folder takes effect after you restart the app.", parent=win)

    def _reset_data_dir(self, win, folder_lbl):
        """Forget the picked folder (and the env var note): back to the default on restart."""
        from tkinter import messagebox
        from engine.tags import set_data_dir_override, data_dir, DATA_DIR_ENV
        import os
        if os.environ.get(DATA_DIR_ENV):
            messagebox.showinfo("Reset to default",
                "The %s environment variable is set: unset it and restart " % DATA_DIR_ENV +
                "to go back to the default folder.", parent=win)
            return
        set_data_dir_override(None)
        folder_lbl.configure(text=self._storage_text())
        self._refresh_welcome_storage()
        messagebox.showinfo("Restart needed",
            "The default app data folder takes effect after you restart the app.", parent=win)

    def set_theme(self, name, save=True):
        """Switch the app between the light and dark theme, live; the choice is kept in
        the app settings."""
        from widgets import set_app_theme
        name = set_app_theme(self, name)
        try:
            self._theme_var.set(name)
        except (tk.TclError, AttributeError):
            pass
        if save:
            try:
                self.tags.settings["theme"] = name
                from engine.tags import save_settings
                save_settings(self.tags.settings)
            except (OSError, ValueError, AttributeError):
                pass
        return name

    def _apply_font_sizes(self, changed):
        """The FONT tokens changed size (tokens.set_font_scale): rebuild the ttk styles
        (fonts, row heights) and the named default fonts, and give every classic widget the
        new size of the font it was made with. (Changing Tk's own scaling instead reaches
        only fonts made afterwards: Tk keeps the pixel size of a font it already made.)"""
        from widgets import setup_theme
        setup_theme(self)
        if not changed:
            return
        by_text = dict((self.tk.call("list", *old) if False else " ".join(
            ("{%s}" % x) if " " in str(x) else str(x) for x in old), new)
            for old, new in changed.items())
        stack = [self]
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
                spec = w.cget("font")
            except (tk.TclError, AttributeError):
                continue
            key = tuple(spec) if isinstance(spec, (tuple, list)) else None
            new = changed.get(key) if key is not None else by_text.get(str(spec))
            if new is None and not isinstance(spec, (tuple, list)) and spec:
                try:
                    new = changed.get(tuple(self.tk.splitlist(spec)))
                    if new is None:
                        parts = self.tk.splitlist(spec)
                        new = changed.get((parts[0], int(parts[1])) + tuple(parts[2:]))
                except (tk.TclError, ValueError, IndexError):
                    new = None
            if new is not None:
                try:
                    w.configure(font=new)
                except tk.TclError:
                    pass

    def set_ui_scale(self, name, save=True):
        """Grow or shrink the interface (View menu > Interface size): every font token
        changes size, at once, in every window. The choice is kept in the app settings and
        applied again at startup."""
        import tokens
        if name not in _UI_SCALE_FACTORS:
            name = "default"
        try:
            factor = _UI_SCALE_FACTORS[name]
            if abs(tokens.font_scale() - factor) > 1e-6:
                self._apply_font_sizes(tokens.set_font_scale(factor))
                self.update_idletasks()
        except (tk.TclError, ValueError, AttributeError):
            pass
        try:
            self._ui_scale_var.set(name)
        except (tk.TclError, AttributeError):
            pass
        if save:
            try:
                self.tags.settings["ui_scale"] = name
                from engine.tags import save_settings
                save_settings(self.tags.settings)
            except (OSError, ValueError, AttributeError):
                pass
        return name

    def _ui_beat(self):
        """Tell the worker threads the Tk thread is serving events (engine.uiyield): while
        it is busy instead, they give way to it."""
        uiyield.beat()
        try:
            self._ui_beat_id = self.after(UI_BEAT_MS, self._ui_beat)
        except tk.TclError:
            uiyield.stop()

    def _warm_tabs(self):
        """Show every tab once while the window is still fully transparent, at start: Tk
        draws a tab's widgets for the first time when the tab is first shown, which made the
        first switch to each tab pause for 100-300 ms. Done here it costs about as much
        once, before the window becomes visible. Only where the window manager supports
        transparency (otherwise the tabs would be seen flicking by)."""
        nb = self._nb
        self.tabs_warmed = []
        if self._windowingsystem not in ("win32", "aqua"):
            return                  # X11: transparency needs a compositor, which may be absent
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            return
        extra = [(self._overview, "Overview"), (self._wal_frame, "WAL")]
        try:
            for w, text in extra:
                nb.add(w, text=text)
            self.update()                       # maps the (transparent) window
            for t in nb.tabs():
                nb.select(t)
                self.update_idletasks()
                self.tabs_warmed.append(nb.tab(t, "text"))
            for w, _text in extra:
                nb.forget(w)
            nb.select(self._search_frame)
            warm_fallback_fonts(self)           # emoji and other scripts: see grid.py
            self.update_idletasks()
        except tk.TclError:
            pass
        finally:
            try:
                self.attributes("-alpha", 1.0)
            except tk.TclError:
                pass

    # ── The active database of the case ──────────────────────────────
    @property
    def db(self):
        """The active database (an empty DB when none is open)."""
        m = self.case.active
        return m.db if m is not None else self._no_db

    @property
    def _count_cache(self):
        """Row counts of the active database's tables."""
        m = self.case.active
        return m.counts if m is not None else self._no_counts

    @_count_cache.setter
    def _count_cache(self, value):
        m = self.case.active
        if m is not None:
            m.counts = value
        else:
            self._no_counts = value

    @property
    def _scope_tables(self):
        """The tables (and views) of the active database a search covers."""
        m = self.case.active
        return m.scope_tables if m is not None else self._no_scope

    @_scope_tables.setter
    def _scope_tables(self, value):
        m = self.case.active
        if m is not None:
            m.scope_tables = value
        else:
            self._no_scope = value

    def member_label(self, member, table=None):
        """How a table is written: 'table' with one database open, 'wa.db › table' in a case."""
        if member is None or not self.case.multi:
            return table if table is not None else (member.name if member else "")
        return member.label(table)

    def _set_app_icon(self):
        """The window and taskbar icon: the logo images in src/assets (bundled as 'assets'
        in the frozen app)."""
        from appicons import window_icons
        try:
            imgs = window_icons(self)
            if imgs:
                self._app_icons = imgs          # Tk drops images Python no longer holds
                self.wm_iconphoto(True, *imgs)
        except tk.TclError:
            pass                                # the icon is cosmetic

    # ── Header ───────────────────────────────────────────────────────
    def _build_header(self):
        """One line, whatever the number of databases: the navigator toggle, the app name,
        the case name and its summary ('com.phonepe.app · 16 databases · 1.2 GB · 247
        tables'), the evidence state as one chip ('16 read-only · 7 WAL merged') with a
        warnings chip beside it when there are any, then the main buttons."""
        hf = ttk.Frame(self, style="Header.TFrame")
        hf.pack(fill="x")
        self._header_frame = hf
        self._header_rule = tk.Frame(self, height=1, background=K["border"])
        self._header_rule.pack(fill="x")
        inner = self._header_inner = ttk.Frame(hf, style="Header.TFrame")
        inner.pack(fill="x", padx=M, pady=S)

        self._nav_btn = ttk.Button(inner, text="☰", width=3, style="Icon.TButton",
                                   command=self._toggle_sidebar_by_hand)
        self._nav_btn.pack(side="left", padx=(0, S))
        # the logo (src/assets), sized for the display scaling
        from appicons import logo_image
        try:
            px = int(round(24 * float(self.tk.call("tk", "scaling")) / (96 / 72.0)))
        except (tk.TclError, ValueError):
            px = 24
        self._logo_img = logo_image(self, px)
        if self._logo_img is not None:
            logo = self._logo = ttk.Label(inner, image=self._logo_img, style="Header.TLabel")
        else:
            logo = self._logo = ttk.Label(inner, text="", style="Header.TLabel")
        logo.pack(side="left")
        # the name, left out in a narrow window (the buttons and the case summary need room)
        self._title_lbl = ttk.Label(inner, text="SQLite GUI Analyzer", style="HeaderTitle.TLabel")
        self._title_lbl.pack(side="left", padx=(S, 0))

        # Buttons on the right (packed before the summary so a narrow window keeps them)
        self._close_btn = ttk.Button(inner, text="Close", command=self._close_db)
        self._close_btn.pack(side="right", padx=(XS, 0))
        self._help_btn = ttk.Button(inner, text="Help", command=lambda: HelpDialog(self))
        self._help_btn.pack(side="right", padx=(XS, 0))
        # Everything the engine had to skip, substitute or guess: a button only when there is
        # something (and always in the Database menu)
        self._issues_btn = ttk.Button(inner, text="Issues", command=self._show_issues)
        self._view_btn = ttk.Button(inner, text="View ▾")
        self._view_menu = tk.Menu(self, tearoff=0)
        self._view_menu.add_radiobutton(label="Light", variable=self._theme_var,
                                        value="light",
                                        command=lambda: self.set_theme("light"))
        self._view_menu.add_radiobutton(label="Dark", variable=self._theme_var,
                                        value="dark",
                                        command=lambda: self.set_theme("dark"))
        self._view_menu.add_separator()
        self._size_menu = tk.Menu(self._view_menu, tearoff=0)
        for _key, _label, _factor in UI_SCALES:
            self._size_menu.add_radiobutton(label=_label, variable=self._ui_scale_var,
                                          value=_key,
                                          command=lambda k=_key: self.set_ui_scale(k))
        self._view_menu.add_cascade(label="Interface size", menu=self._size_menu)
        self._view_menu.add_separator()
        self._view_menu.add_command(label="Storage\u2026", command=self._show_storage)
        self._view_btn.configure(command=lambda: self._post_menu(self._view_menu,
                                                                 self._view_btn))
        self._view_btn.pack(side="right", padx=(XS, 0))
        self._db_btn = ttk.Button(inner, text="Database ▾")
        self._db_menu = tk.Menu(self, tearoff=0, postcommand=self._fill_db_menu)
        self._db_btn.configure(command=lambda: self._post_menu(self._db_menu, self._db_btn))
        self._db_btn.pack(side="right", padx=(XS, 0))
        # Open ▾: a database, a folder of databases, more databases, the recent ones
        self._open_btn = ttk.Button(inner, text="Open ▾", style="Primary.TButton")
        self._open_menu = tk.Menu(self, tearoff=0, postcommand=self._fill_open_menu)
        self._open_btn.configure(command=lambda: self._post_menu(self._open_menu,
                                                                 self._open_btn))
        self._open_btn.pack(side="right", padx=(XS, 0))
        # the command palette: every database, table, column, tab and action
        self._palette_btn = ttk.Button(inner, text="Go to…  Ctrl+K", style="Subtle.TButton",
                                       command=self.open_palette)
        self._palette_btn.pack(side="right", padx=(XS, 0))

        # the evidence state (one chip) and the warnings (a chip only when there are some)
        self._warn_chip = Chip(inner, "", bg=K["card"], active=True,
                               on_click=lambda: self._show_status_detail())
        self._evidence_chip = Chip(inner, "", bg=K["card"],
                                   on_click=lambda: self._show_status_detail())
        self._evidence_tip = ToolTip(self._evidence_chip, "")
        # the case (or database) name and its summary, shortened in the middle to fit
        self._case_lbl = ttk.Label(inner, text="", style="Header.TLabel", font=F["body_bold"])
        # (the name leads the summary line, which shortens itself to fit: the header never
        # cuts a control, however narrow the window)
        self._db_info = ElideLabel(inner, text="No database loaded", style="Header.TLabel")
        self._db_info.pack(side="left", fill="x", expand=True, padx=(M, S))
        self._banners = []

    def _fill_open_menu(self):
        m = self._open_menu
        m.delete(0, "end")
        m.add_command(label="Open database…", accelerator="Ctrl+O", command=self._open_file)
        m.add_command(label="Open with Safe parse…", command=lambda: self._open_file(safe=True))
        m.add_command(label="Open folder…", command=self._open_folder)
        m.add_command(label="Add database(s)…", command=self._add_databases)
        m.add_separator()
        m.add_cascade(label="Recent", menu=self.tags.recent_menu())

    def _update_welcome(self, have_databases):
        """The welcome screen over the tabs while nothing is open: what the tool does, the two
        ways to start and the read-only promise; gone as soon as a database is open."""
        w = getattr(self, "_welcome", None)
        if have_databases:
            if w is not None and w.winfo_manager():
                w.place_forget()
            return
        if w is None:
            from appicons import illustration
            # a plain page over the whole tab area, the card centred on it (the empty tabs
            # behind would only be noise)
            outer = self._welcome = ttk.Frame(self._main)
            w = ttk.Frame(outer, style="Card.TFrame", padding=28)
            w.place(relx=0.5, rely=0.45, anchor="center")
            outer.art = w.art = illustration(self, "welcome", 260)
            if w.art is not None:
                ttk.Label(w, image=w.art, style="Card.TLabel").pack()
            ttk.Label(w, text="Open a SQLite database to begin", style="CardTitle.TLabel").pack(
                pady=(12, 4))
            ttk.Label(w, text="Search every table, browse millions of rows, decode BLOBs and "
                              "timestamps, and recover deleted records from the WAL and freed "
                              "pages. Files are opened read-only: nothing is ever written next "
                              "to them.", style="CardMuted.TLabel", wraplength=460,
                      justify="center").pack()
            row = ttk.Frame(w, style="Plain.TFrame")
            row.pack(pady=(14, 0))
            ttk.Button(row, text="Open database…", style="Primary.TButton",
                       command=self._open_file).pack(side="left", padx=4)
            ttk.Button(row, text="Open folder…", command=self._open_folder).pack(
                side="left", padx=4)
            ttk.Label(w, text="Ctrl+O opens a database · a folder lists every SQLite file "
                              "in it, whatever its name", style="CardMuted.TLabel").pack(
                pady=(10, 0))
            # where the tool keeps what it saves (never next to the evidence)
            store = ttk.Frame(w, style="Plain.TFrame")
            store.pack(fill="x", pady=(18, 0))
            ttk.Separator(store).pack(fill="x", pady=(0, 10))
            ttk.Label(store, text="Your tags, notes, recent files and settings are saved in",
                      style="CardMuted.TLabel").pack()
            self._welcome_store = ttk.Label(store, style="Card.TLabel", wraplength=460,
                                            justify="center")
            self._welcome_store.pack(pady=(2, 6))
            sb = ttk.Frame(store, style="Plain.TFrame")
            sb.pack()
            ttk.Button(sb, text="Open folder", style="Small.TButton",
                       command=lambda: self.open_data_folder()).pack(side="left", padx=4)
            ttk.Button(sb, text="Change…", style="Small.TButton",
                       command=self._show_storage).pack(side="left", padx=4)
            w = outer
        self._refresh_welcome_storage()
        if not w.winfo_manager():
            w.place(x=0, y=0, relwidth=1, relheight=1)
        w.lift()

    def _refresh_header(self):
        """The case name, its summary and the evidence chips (after any change of the case)."""
        ms = [m for m in self.case if m.db.ok]
        self._update_welcome(bool(ms))
        chips = (self._warn_chip, self._evidence_chip)
        for c in chips:
            if c.winfo_manager():
                c.pack_forget()
        self._open_btn.configure(style="TButton" if ms else "Primary.TButton")
        if not ms:
            self._case_lbl.configure(text="")
            return
        total = sum((m.size or 0) for m in ms)
        tables = sum(len(m.db.tables()) for m in ms)
        if len(ms) > 1:
            name = case_name([m.path for m in ms])
            self._case_lbl.configure(text=name if len(name) <= 28 else name[:27] + "…")
            self._db_info.set_text("%s · %d databases · %s · %s tables · active: %s" % (
                name, len(ms), fmtb(total), format(tables, ","), self.case.active.name))
        else:
            m = ms[0]
            self._case_lbl.configure(text=m.name if len(m.name) <= 28 else m.name[:27] + "…")
            self._db_info.set_text("%s · %s · %d tables · loaded in %.2fs" % (
                m.path, fmtb(total), tables, m.load_time))
        modes = OrderedDict()
        for m in ms:
            key = mode_label(m.db.mode)
            modes[key] = modes.get(key, 0) + 1
        if len(ms) > 1:
            text = "%d read-only" % len(ms)
            merged = modes.get(mode_label("ram-overlay"), 0)
            if merged:
                text += " · %d WAL merged" % merged
            safe = modes.get(mode_label("safe-parse"), 0)
            if safe:
                text += " · %d Safe parse" % safe
        else:
            text = "Read-only · %s" % mode_label(ms[0].db.mode)
        self._evidence_chip.set(text=text)
        self._evidence_tip.text = "Every database is opened read-only (nothing is written " \
            "next to the evidence):\n" + "\n".join("  %d: %s" % (n, k)
                                                   for k, n in modes.items()) + \
            "\nClick for the status of every database."
        warns = sum(len(member_warnings(m)) for m in ms)
        # packed right after the palette button: to its left, and before the summary gets
        # its share of the line
        last = self._palette_btn
        if warns:
            self._warn_chip.set(text="⚠ %d warning%s" % (warns, "" if warns == 1 else "s"))
            self._warn_chip.pack(side="right", padx=(XS, S), after=last)
            last = self._warn_chip
        self._evidence_chip.pack(side="right", padx=(S, 0), after=last)

    def _fill_db_menu(self):
        """The Database menu: what there is to know about the open database(s)."""
        m = self._db_menu
        m.delete(0, "end")
        ok = self.db.ok
        st = "normal" if ok else "disabled"
        n = self._issue_count()
        m.add_command(label="Info", command=self._show_info, state=st)
        m.add_command(label="Evidence and verification…", command=self._show_evidence,
                      state=st)
        m.add_command(label="Issues (%d)…" % n if n else "Issues…", command=self._show_issues,
                      state=st)
        m.add_command(label="Activity log…", command=self._show_activity, state=st)
        m.add_separator()
        m.add_command(label="Schema report (HTML)…", command=self._schema_export_html, state=st)
        m.add_command(label="Database Map…", command=lambda: self.datamap.export_map(),
                      state=st)
        m.add_separator()
        m.add_command(label="Limits…", command=lambda: self.datamap.limits_window(self))

    # ── Body ─────────────────────────────────────────────────────────
    def _build_body(self):
        """The Case navigator on the left (resizable, Ctrl+B hides it), the tabs on the
        right."""
        body = self._body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        self._sidebar_visible = True
        self._navigator = CaseNavigator(body, self)
        self._navigator.configure(width=300)
        body.add(self._navigator, weight=0)
        body.bind("<ButtonRelease-1>", self._nav_sash_moved, add="+")
        main = self._main = ttk.Frame(body)
        body.add(main, weight=1)

        # a thin strip at the navigator's edge: ◀ folds it, ▶ (then at the window's edge)
        # brings it back - always in view, like the ☰ button and Ctrl+B
        rail = self._nav_rail = tk.Button(
            main, text="◀", font=F["small"], width=2, bd=0, relief="flat",
            bg=K["muted"], fg=K["muted_text"], activebackground=K["hover"],
            activeforeground=K["text"], cursor="hand2", takefocus=0, highlightthickness=0,
            command=self._toggle_sidebar_by_hand)
        rail.pack(side="left", fill="y")
        self._nav_rail_tip = ToolTip(rail, "Hide the databases panel (Ctrl+B)")

        self._nb = ttk.Notebook(main)
        self._nb.pack(fill="both", expand=True)

        self._search_frame = ttk.Frame(self._nb)
        self._browse_frame = ttk.Frame(self._nb)

        self._nb.add(self._search_frame, text="Search")
        self._nb.add(self._browse_frame, text="Browse")

        # WAL tab: inserted after Browse for a database with a -wal file (also one that cannot
        # be read: the tab then says why)
        self._wal_frame = WalTab(self._nb, self)
        self._wal_tab_added = False

        # SQL Query Editor tab — always present
        self._sql_frame = ttk.Frame(self._nb)
        self._nb.add(self._sql_frame, text="SQL")

        # Forensics tab (deleted records, row history, dropped tables, journal, audit)
        self._forensics = ForensicsTab(self._nb, self)
        self._nb.add(self._forensics, text="Forensics")

        # Timeline tab (every dated row in time order)
        self._timeline = TimelineTab(self._nb, self)
        self._nb.add(self._timeline, text="Timeline")

        self._build_sql_tab()
        self._build_search_tab()
        self._build_browse_tab()
        # the tabs that work on one database say which in a breadcrumb bar at their top
        from breadcrumb import Breadcrumb
        self._crumbs = [self._browse_crumb]
        for frame in (self._sql_frame, self._forensics, self._wal_frame):
            kids = frame.pack_slaves()
            crumb = Breadcrumb(frame, self)
            if kids:
                crumb.pack(fill="x", padx=M, pady=(S, 0), before=kids[0])
            else:
                crumb.pack(fill="x", padx=M, pady=(S, 0))
            self._crumbs.append(crumb)

    # ── The Case navigator's actions ─────────────────────────────────
    def browse_member_table(self, member, table=None):
        """Open a table of a database in Browse (its database made active); table None: the
        database's current (or first) table."""
        if member is None or member not in self.case.members:
            return
        self.activate_member(member)
        self._nb.select(self._browse_frame)
        if table:
            self._browse_table_var.set(table)
            self._load_browse_table()

    def search_in_table(self, member, table):
        self._schema_ctx_search(table, member)

    def show_in_folder(self, path):
        """Open the folder of a database in the file manager with the file selected (only
        opens a window there; nothing is written)."""
        import subprocess
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError as e:
            messagebox.showerror("Show in folder", str(e), parent=self)

    def _flash_schema(self, text):
        """Say in the navigator what a Copy did (cleared after a few seconds)."""
        self._navigator.flash(text)

    def _populate_schema(self):
        """List the case in the navigator again."""
        self._navigator.rebuild()

    def _schema_export_html(self):
        """Export full schema report as HTML."""
        if not self.db.ok:
            return
        tables = self.db.tables()
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            initialfile="schema_report.html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")])
        if not write_allowed(path):
            return
        db, counts = self.db, dict(self._count_cache)
        from engine.export import evidence_record, provenance, write_manifest

        def work(job):
            job.status = "Reading the evidence hashes…"
            files = evidence_record(db.evidence, True, lambda: job.cancelled)
            job.status = "Writing the schema report…"
            html = _build_schema_html(db, db.evidence.main, VERSION, tables=tables,
                                      row_counts=counts, evidence=files)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            info = provenance(VERSION, [(db.evidence.main, files)], "schema report",
                              rows=len(tables))
            return write_manifest(path, info, [path], True, self.case.is_protected)

        def done(manifest, error, cancelled):
            if error is not None:
                messagebox.showerror("Schema report", str(error), parent=self)
                return
            self.activity("export", what="schema report", path=path, manifest=manifest)
            messagebox.showinfo("Schema report", "%d tables documented in:\n%s\n\nManifest:"
                                "\n%s" % (len(tables), path, manifest), parent=self)
        Job(self, "Schema report", work, done, release=self._release_worker_connection,
            members=[self.case.active])

    def _schema_ctx_search(self, tbl, member=None):
        """Search only this table (of this database, in a case)."""
        member = member if member is not None else self.case.active
        if member is not None:
            member.scope_tables = [tbl]
            if self.case.multi:
                # only this database for the Search tab (the other features keep theirs)
                self.scopes.set("search", [member.uid], own=True)
        self._update_scope_chip()
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()

    def _copy_schema(self):
        """Copy the schema of every table of the active database."""
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
        self._flash_schema("Copied the schema of %d tables" % len(self.db.tables()))

    # ── Search Tab ───────────────────────────────────────────────────
    def _build_search_tab(self):
        sf = self._search_frame

        # Row 1: the term, the mode, Search and Stop (the rows wrap in a narrow window)
        top = self._search_top = FlowFrame(sf)
        top.pack(fill="x", padx=M, pady=(M, XS))
        self._search_var = tk.StringVar()
        self._search_entry = top.add(ttk.Entry(top, textvariable=self._search_var, width=40,
                                               style="Search.TEntry"), stretch=True)
        add_placeholder(self._search_entry, self._search_var,
                        "Search the text of every table in scope…", font=F["large"])
        self._search_mode_var = tk.StringVar(value="Case-Insensitive")
        # the nine modes in their groups (Text, Binary, Schema), searchable
        labels = dict((key, label) for label, key in SEARCH_MODES.items())
        self._mode_combo = top.add(SearchableCombobox(
            top, textvariable=self._search_mode_var, state="readonly", width=18,
            groups=[(group, [labels[k] for k in keys]) for group, keys in SEARCH_MODE_GROUPS]),
            gap=S)
        self._mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        self._search_btn = top.add(ttk.Button(top, text="Search", style="Primary.TButton",
                                              command=self._do_search), gap=S)
        self._stop_btn = top.add(ttk.Button(top, text="Stop", command=self._stop_search),
                                 visible=False)

        # Row 2: where a search looks and what it includes; the rest behind Advanced ▾
        opts = self._search_opts = Toolbar(sf)
        opts.pack(fill="x", padx=M, pady=(0, XS))
        # which databases a search covers: shown only in a case of 2+ databases
        self._search_db_picker = opts.add(
            ScopePicker(opts, self, "search", on_change=self._search_dbs_changed),
            visible=False)
        self._scope_btn = opts.add(ttk.Button(opts, text="All tables ▾",
                                              command=self._show_scope), gap=S)
        self._reset_scope_btn = opts.add(ttk.Button(opts, text="Reset", style="Link.TButton",
                                                    command=self._reset_scope), gap=XS,
                                         visible=False)
        self._scope_chip = self._scope_btn          # says the table scope itself
        self._deep_blob_var = tk.BooleanVar(value=False)
        self._search_decoded_var = tk.BooleanVar(value=False)
        self._search_views_var = tk.BooleanVar(value=False)
        self._search_view_names = set()
        # offered only when a searched database has WAL frames / freed pages
        self._search_wal_var = tk.BooleanVar(value=False)
        self._search_wal_cb = opts.add(ttk.Checkbutton(opts, text="WAL row versions",
                                                       variable=self._search_wal_var),
                                       gap=M, visible=False)
        self._search_free_var = tk.BooleanVar(value=False)
        self._search_free_cb = opts.add(ttk.Checkbutton(opts, text="Freed pages",
                                                        variable=self._search_free_var),
                                        gap=S, visible=False)
        self._limit_var = tk.StringVar(value="500")
        self._search_adv_btn, adv = menu_button(opts, "Advanced ▾")
        opts.add(self._search_adv_btn, gap=M)
        self._search_adv_menu = adv
        adv.add_checkbutton(label="Include BLOB bytes (UTF-8, UTF-16, hex)",
                            variable=self._deep_blob_var)
        adv.add_checkbutton(label="Include decoded BLOBs (plists, protobuf, JSON, gzip…)",
                            variable=self._search_decoded_var)
        adv.add_checkbutton(label="Include views", variable=self._search_views_var)
        adv.add_separator()
        lim = tk.Menu(adv, tearoff=0)
        for v in ("100", "500", "1000", "5000", "All"):
            lim.add_radiobutton(label=v, value=v, variable=self._limit_var,
                                command=self._update_adv_label)
        adv.add_cascade(label="Max matching rows per table", menu=lim)
        for var in (self._deep_blob_var, self._search_decoded_var, self._search_views_var):
            var.trace_add("write", lambda *a: self._update_adv_label())
        self._search_export_btn = opts.add(ttk.Button(opts, text="Export ▾"), gap=M)
        self._search_export_menu = tk.Menu(self, tearoff=0)
        self._search_export_menu.add_command(label="Matches (CSV or JSON)…",
                                             command=self._search_export_csv)
        self._search_export_menu.add_command(label="Matches with their whole rows (JSON)…",
                                             command=self._search_export_details)
        self._search_export_menu.add_command(label="Copy results",
                                             command=self._search_copy)
        self._search_export_btn.configure(command=lambda: self._post_menu(
            self._search_export_menu, self._search_export_btn))
        # errors: a button only when a table could not be searched
        self._search_err_btn = opts.add(ttk.Button(opts, text="", style="Small.TButton",
                                                   command=self._show_search_errors),
                                        gap=S, visible=False)
        self._search_btn_row = opts

        # what the chosen mode matches (only for the modes that need saying)
        self._hint_label = wrap_to_width(ttk.Label(sf, text="", style="Muted.TLabel"))
        self._hint_label.pack(fill="x", padx=M + XS, pady=(0, XS))

        # Progress (a thin bar), and the status: one line, the details folded away
        prog_f = ttk.Frame(sf)
        prog_f.pack(fill="x", padx=M, pady=(XS, 0))
        self._search_progress = ttk.Progressbar(prog_f, mode="determinate")
        # shown only while a search runs (nothing looks busy at idle)
        self._search_prog_f = prog_f
        self._search_status = StatusLine(sf)
        self._search_status.set("Type text above and press Search \u2014 every table in scope is scanned.")
        self._search_status.pack(fill="x", padx=M, pady=(XS, XS))

        # Filters of the results (paging is under the results)
        self._sr_page = 0
        self._sr_page_size = 200
        self._sr_filtered = []
        self._search_start_time = time.time()

        # Filters of the results: searchable dropdowns (the Table one lists each table with
        # its rows found), the Database one only in a case of 2+ databases
        fp_bar = self._sr_filter_bar = FlowFrame(sf)
        fp_bar.pack(fill="x", padx=M, pady=(XS, XS))

        def dropdown(label, width, values=("All",), visible=True, gap=S):
            lbl = fp_bar.add(ttk.Label(fp_bar, text=label, style="Muted.TLabel"), gap=gap,
                             visible=visible)
            combo = fp_bar.add(SearchableCombobox(fp_bar, values=list(values),
                                                  state="readonly", width=width),
                               gap=XS, visible=visible)
            combo.set("All")
            combo.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())
            return lbl, combo
        self._sr_db_lbl, self._sr_db_filter = dropdown("Database", 16, visible=False, gap=0)
        self._sr_source_lbl, self._sr_source_filter = dropdown(
            "Source", 11, ["All", "DB", "WAL", source_name("Freelist")])
        _l, self._sr_table_filter = dropdown("Table", 30)
        _l, self._sr_col_filter = dropdown("Column", 20)
        _l, self._sr_type_filter = dropdown("Type", 8)

        # One line per row (its matching cells and WAL copies listed under it), or one per cell
        self._sr_group_var = tk.BooleanVar(value=True)
        self._sr_group_cb = fp_bar.add(ttk.Checkbutton(fp_bar, text="One line per row",
                                                       variable=self._sr_group_var,
                                                       command=self._filter_search_results),
                                       gap=M)

        # Paging, under the results
        self._sr_page_bar = ttk.Frame(sf)
        self._sr_page_bar.pack(side="bottom", fill="x", padx=M, pady=(0, S))
        ttk.Button(self._sr_page_bar, text="◀", width=3, style="Small.TButton",
                   command=self._sr_prev_page).pack(side="left", padx=1)
        ttk.Button(self._sr_page_bar, text="▶", width=3, style="Small.TButton",
                   command=self._sr_next_page).pack(side="left", padx=1)
        self._sr_page_label = ttk.Label(self._sr_page_bar, text="", style="Muted.TLabel")
        self._sr_page_label.pack(side="left", padx=XS)

        # Results treeview in a bordered card
        border_frame = tk.Frame(sf, relief="flat", bd=0, bg=K["border"], padx=1, pady=1)
        border_frame.pack(fill="both", expand=True, padx=M, pady=(XS, XS))

        cols = ("#", "Source", "Table", "Column", "RowID", "Matched Value", "Type")
        self._search_tree = ttk.Treeview(border_frame, columns=cols, show="tree headings",
                                         selectmode="browse")
        for c in cols:
            self._search_tree.heading(c, text=c)
        self._search_tree.column("#0", width=24, minwidth=24, stretch=False)   # expand arrow
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
        self._sr_reset_groups()

    def _sr_reset_groups(self):
        """Forget the grouped view of the results (a new search, or the DB closed)."""
        self._sr_grouper = ResultGrouper()
        self._sr_grouped_upto = 0        # _search_results[:n] are filed in _sr_grouper
        self._sr_groups_filtered = []
        self._sr_iid_map = {}            # tree item -> (hit, RowGroup or None)
        self._search_table_hits = {}     # table -> its hits, kept in table order at the end
        self._search_wal_hits = []

    def _sr_ingest(self):
        """File the hits the search worker added since the last call (Tk thread only)."""
        results = self._search_results
        n = len(results)
        for i in range(self._sr_grouped_upto, n):
            self._sr_grouper.add(results[i])
        self._sr_grouped_upto = n

    def _on_mode_change(self, event=None):
        mode = self._search_mode_var.get()
        mk = SEARCH_MODES.get(mode, "ci")
        if mk == "rx":
            self._hint_label.configure(
                text="Tip: Type \\. for literal dot (not \\\\.). Omit ^ and $ to find patterns within values.",
                foreground=C["green"])
        elif mk == "blob":
            self._hint_label.configure(
                text="Finds text inside BLOBs as UTF-8, UTF-16LE and UTF-16BE (any case); with "
                     "'Include BLOB bytes' a term like 'ff d8 ff' is also matched as bytes.",
                foreground=C["orange"])
        elif mk == "hex":
            self._hint_label.configure(
                text="Byte pattern, byte-aligned: 'ff d8 ff', '0x1f8b', '\\x89PNG', 'de ad ?? ef' "
                     "(?? = any byte). Searches BLOBs and text as stored bytes.",
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
        try:
            check_term(term, search_mode_key(self._search_mode_var.get()))
        except ValueError as e:        # a malformed hex pattern or regular expression
            self._hint_label.configure(text="Cannot search: %s" % e, foreground=C["red"])
            return
        self._on_mode_change()
        # a search still finishing is stopped and ignored (its generation is old): its hits
        # never mix into this one's, and the Tk thread does not wait for it
        self._stop_search()
        self._search_gen += 1
        self._search_cancel = False
        self._search_results = []
        self._search_errors = []
        self._search_capped = []        # (uid, where, rows kept) of the tables the limit cut
        self._search_removed = []       # databases removed from the case since (their names)
        self._sr_reset_groups()
        self._sr_filtered = []
        self._sr_page = 0
        self._search_tree.delete(*self._search_tree.get_children())
        self._refresh_search_errors()
        self._search_start_time = time.time()
        for combo in (self._sr_db_filter, self._sr_source_filter, self._sr_table_filter, self._sr_col_filter,
                      self._sr_type_filter):
            combo.set("All")

        mode = self._search_mode_var.get()
        self._search_term = term
        self._search_mode_key = search_mode_key(mode)
        lv = self._limit_var.get()
        limit = None if lv == "All" else int(lv)        # All: no limit at all
        self._search_limit = limit
        deep = self._deep_blob_var.get()
        decoded = self._search_decoded_var.get()
        freelist = self._search_free_var.get()
        wal = self._search_wal_var.get()
        self._search_view_names = set()
        work = [(m, self._search_scope_names(m)) for m in self._search_members()]
        self._search_work = work
        self._search_tables = [(m.uid, t) for m, tables in work for t in tables]
        total = len(self._search_tables)

        self._search_progress.configure(maximum=max(total, 1), value=0)
        self._search_progress.pack(fill="x", expand=True)
        self._search_status.set(*self._search_scope_text(work))

        self._search_thread = threading.Thread(
            target=self._search_worker, args=(work, term, mode, limit, deep, decoded, freelist,
                                              wal),
            daemon=True)
        self._search_thread.start()
        self._search_top.show(self._stop_btn, True)
        self.activity("search", term=term, mode=search_mode_key(mode),
                      limit=limit if limit is not None else "all", databases=[
                          m.path for m, _t in work], tables=total,
                      options=dict(blob_bytes=deep, decoded=decoded, freed_pages=freelist,
                                   wal=wal, views=self._search_views_var.get()))

    def _search_members(self):
        """The databases a search covers: the open one, or in a case those the 'Databases:'
        button ticks (all by default)."""
        if not self.case.multi:
            return [self.case.active] if self.case.active is not None else []
        return [m for m in self.scopes.members("search") if m.db.ok]

    def _search_scope_text(self, work=None, verb="Searching"):
        """('Searching 12 tables…', details) or in a case ('Searching 2 of 3 databases ·
        40 tables…', [the databases]) - where the search looks, always said before it
        starts."""
        if work is None:
            self._search_view_names = set()
            work = [(m, self._search_scope_names(m)) for m in self._search_members()]
        n = sum(len(t) for _m, t in work)
        if not self.case.multi:
            return "%s %s…" % (verb, plural(n, "table")), []
        names = [m.name for m, _t in work]
        summary = "%s %s · %s…" % (verb, scope_text(len(work), len(self.case)),
                                   plural(n, "table"))
        return summary, ["Databases: " + ", ".join(names)]

    def _search_dbs_changed(self):
        """The Search tab's scope changed (it is saved with the case): show it."""
        self._update_search_options()
        if not (self._search_thread and self._search_thread.is_alive()):
            summary, details = self._search_scope_text(verb="Will search")
            self._search_status.set(summary.rstrip("…"), details)

    def _update_adv_label(self):
        """'Advanced ▾', or 'Advanced (2) ▾' while options there are on."""
        n = sum(1 for v in (self._deep_blob_var, self._search_decoded_var,
                            self._search_views_var) if v.get())
        n += self._limit_var.get() != "500"
        self._search_adv_btn.configure(text="Advanced (%d) ▾" % n if n else "Advanced ▾")

    def _update_search_options(self):
        """'Include WAL row versions' and 'Include freed pages' are offered only when a
        searched database has WAL frames / freed pages."""
        members = self._search_members()
        has_wal = any(m.db.has_wal for m in members)
        self._search_opts.show(self._search_wal_cb, has_wal)
        if not has_wal:
            self._search_wal_var.set(False)
        has_free = any(m.db.freelist_count() > 0 for m in members)
        self._search_opts.show(self._search_free_cb, has_free)
        if not has_free:
            self._search_free_var.set(False)
        self._update_scope_chip()

    def _update_scope_chip(self):
        """The table scope button says what it covers: 'All tables ▾', or '12 of 19 tables ▾'
        when narrowed (then Reset shows); the tooltip names the databases narrowed."""
        parts, n_all, n_in = [], 0, 0
        for m in self._search_members():
            tables = m.db.tables()
            scope = set(m.scope_tables or ())
            n = len([t for t in tables if not scope or t in scope])
            n_all += len(tables)
            n_in += n
            if n != len(tables):
                parts.append(("%s: " % m.name if self.case.multi else "") +
                              "%d of %d tables" % (n, len(tables)))
        self._scope_btn.configure(text=("%s of %s tables ▾" % (format(n_in, ","),
                                                             format(n_all, ","))) if parts
                                  else "All tables ▾")
        self._search_opts.show(self._reset_scope_btn, bool(parts))
        tip = getattr(self, "_scope_btn_tip", None)
        if tip is None:
            tip = self._scope_btn_tip = ToolTip(self._scope_btn, "")
        tip.text = "Choose which tables (and views) a search covers" + (
            ":\n" + "\n".join(parts) if parts else "")

    def _after_safe(self, ms, func, *args):
        """after() called from a worker thread: if the interpreter is already gone
        (a straggler past _stop_workers, or a late tick after destroy()), after()
        raises in the worker. Swallow it: the UI update is simply lost."""
        try:
            self.after(ms, func, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _search_worker(self, work, term, mode, limit, deep, decoded=False, freelist=False,
                       wal=False):
        """Search each database of `work` [(member, tables)]: its tables in parallel (the
        engine runs several SQLite scans at once), then its WAL row versions and freed pages
        when asked. Every hit carries its database (dbid, database). Hits are appended to
        _search_results; the Tk thread groups and shows them."""
        total = sum(len(t) for _m, t in work)
        # this search's own lists and generation: a search replaced by a newer one (still
        # finishing an interrupted statement) stops and never mixes into the new results
        gen = self._search_gen
        results, errors, table_hits = (self._search_results, self._search_errors,
                                       self._search_table_hits)
        capped = self._search_capped
        cancel = lambda: self._search_cancel or gen != self._search_gen   # noqa: E731

        # one matching row more than the limit is read, so a place with exactly `limit`
        # matching rows is not taken for one that has more
        probe = None if limit is None else limit + 1

        def keep(m, where, hits, key):
            """The hits of a table (or WAL / freed pages) up to `limit` matching rows; when
            there were more, the place is noted as stopped at the limit."""
            if limit is None or not hits:
                return hits
            seen, out = set(), []
            for h in hits:
                k = key(h)
                if k not in seen:
                    if len(seen) >= limit:
                        continue            # a row past the limit: only says there are more
                    seen.add(k)
                out.append(h)
            if len(out) < len(hits):
                capped.append((m.uid, where, limit))
            return out
        mode_key = search_mode_key(mode)
        multi = len(self.case) > 1
        done_dbs = self._search_done_dbs = []
        # per database: [tables searched, tables, what it is doing]
        progress = self._search_db_progress = OrderedDict(
            (m.uid, [0, len(t), "waiting"]) for m, t in work)
        last = [0.0]
        wal_hits = OrderedDict((m.uid, []) for m, _t in work)

        def tick():
            now = time.time()
            if now - last[0] >= 0.3 and gen == self._search_gen:
                last[0] = now
                self._after_safe(0, self._update_search_ui,
                                         sum(p[0] for p in progress.values()), total)

        def one(m, tables):
            db = m.db
            prefix = (m.name + ": ") if multi else ""
            prog = progress[m.uid]
            prog[2] = "tables"
            try:
                for tbl, hits, err in db.search_tables(
                        tables, term, mode, probe if mode_key != "col" else limit, deep,
                        cancel, decoded=decoded):
                    prog[0] += 1
                    if err is not None:
                        errors.append(f"{prefix}{tbl}: {err}")
                    if hits and mode_key != "col":
                        hits = keep(m, tbl, hits, lambda h: repr(h.get("locator")))
                    for h in hits:
                        h["source"] = "DB"
                        h["dbid"], h["database"] = m.uid, m.name
                    if hits:
                        table_hits[(m.uid, tbl)] = hits
                        results.extend(hits)
                    tick()
            except Exception as e:
                if not cancel():
                    errors.append(f"{prefix}search: {e}")

            # Hidden data: every row version kept in WAL frames (identical copies searched once)
            if wal and db.has_wal and not cancel():
                prog[2] = "WAL frames"
                tick()
                got = []
                try:
                    for result in db.wal.search(term, mode, limit=probe, cancel=cancel,
                                                deep_blob=deep, decoded=decoded):
                        if cancel():
                            break
                        result["dbid"], result["database"] = m.uid, m.name
                        got.append(result)
                        tick()
                except Exception as e:
                    errors.append(f"{prefix}WAL: {e}")
                got = keep(m, "WAL row versions", got, lambda h: id(h.get("frames")))
                wal_hits[m.uid].extend(got)
                results.extend(got)

            # Deleted records still held by freed pages
            if freelist and db.freelist_count() > 0 and not cancel():
                prog[2] = "freed pages"
                tick()
                got = []
                try:
                    for result in db.search_freelist(term, mode, limit=probe, deep_blob=deep,
                                                     cancel=cancel, decoded=decoded):
                        if cancel():
                            break
                        result["dbid"], result["database"] = m.uid, m.name
                        got.append(result)
                except Exception as e:
                    errors.append(f"{prefix}freed pages: {e}")
                got = keep(m, source_name("Freelist"), got,
                           lambda h: (h.get("page"), h.get("cell_offset")))
                wal_hits[m.uid].extend(got)          # after the tables, in order
                results.extend(got)
            prog[2] = "stopped" if cancel() else "done"
            if not cancel():
                done_dbs.append(m.uid)
            tick()

        if len(work) <= 1:
            for m, tables in work:
                one(m, tables)
        else:
            # several databases side by side (each one's tables in parallel as well); every
            # thread closes its connections when its database is done
            def task(m, tables):
                self._search_threads.append(threading.current_thread())
                try:
                    one(m, tables)
                finally:
                    self._release_worker_connection()
            self._search_threads = []
            with ThreadPoolExecutor(max_workers=min(limits.get("case_search_parallel"),
                                                    len(work))) as ex:
                for f in [ex.submit(task, m, t) for m, t in work]:
                    try:
                        f.result()
                    except Exception as e:      # reported, never lost
                        errors.append(f"search: {e}")
        if gen != self._search_gen:
            return                              # replaced by a newer search
        for uid, hits in wal_hits.items():      # in database order
            self._search_wal_hits.extend(hits)

        self._after_safe(0, self._finalize_search, total)

    def _update_search_ui(self, done, total):
        self._sr_ingest()
        elapsed = time.time() - self._search_start_time
        text = (f"Found {len(self._search_results):,} matches in {len(self._sr_grouper.groups):,} rows"
                f"… ({done:,}/{total:,} tables, {elapsed:.1f}s)")
        details = []
        if self.case.multi:
            # each database's progress: tables searched, then WAL / freed pages, done
            prog = getattr(self, "_search_db_progress", {})
            finished = []
            for m, _t in getattr(self, "_search_work", []):
                p = prog.get(m.uid)
                if p is None:
                    continue
                if p[2] == "done":
                    finished.append(m.name)
                elif p[2] in ("tables", "waiting"):
                    details.append("%s: %d/%d tables" % (m.name, p[0], p[1]))
                else:
                    details.append("%s: %s" % (m.name, p[2]))
            if finished:
                text += " · %d of %d databases done" % (len(finished), len(prog))
                details.append("%d done: %s" % (len(finished), ", ".join(finished)))
        self._search_status.set(text, details)
        self._refresh_search_errors()
        self._search_progress.configure(value=done)
        # Progressive display: fill the first page while the search runs, then leave it alone
        # so a row the user is looking at does not jump.
        self._sr_filtered = self._search_results
        self._sr_groups_filtered = self._sr_grouper.groups
        if self._sr_page == 0 and len(self._search_tree.get_children()) < self._sr_page_size:
            self._display_search_page(searching=True)
        else:
            self._update_sr_page_label(searching=True)

    def _finalize_search(self, total):
        searched = sum(p[0] for p in getattr(self, "_search_db_progress", {}).values())
        if self._search_cancel:
            status = "Stopped after %s of %s" % (format(min(searched, total), ","),
                                                  plural(total, "table"))
        else:
            status = "Complete"
        # Results arrive in the order tables finish: show them in table order
        tables = getattr(self, "_search_tables", [])
        ordered = []
        for key in tables:
            ordered.extend(self._search_table_hits.get(key, []))
        ordered.extend(self._search_wal_hits)
        self._search_results = ordered
        self._sr_grouper = ResultGrouper()
        self._sr_grouped_upto = 0
        self._sr_ingest()
        groups = self._sr_grouper.groups

        tc = {}            # (dbid, source or None, table) -> rows found
        for g in groups:
            key = (g.dbid, None if g.source == "DB" else g.source, g.table)
            tc[key] = tc.get(key, 0) + 1
        order = dict((m.uid, i) for i, m in enumerate(self.case))
        labels = OrderedDict()
        cut = set((uid, where) for uid, where, _n in getattr(self, "_search_capped", ()))
        for key in sorted(tc, key=lambda k: (order.get(k[0], 0), k[2].lower(), k[1] or "")):
            dbid, src, table = key
            name = table if src is None else f"{source_name(src)}: {table}"
            plus = "+" if src is None and (dbid, table) in cut else ""   # stopped at the limit
            labels[f"{self.member_label(self.case.find(dbid), name)} ({tc[key]}{plus})"] = key
        self._sr_table_labels = labels
        self._search_table_counts = dict((label, tc[key]) for label, key in labels.items())
        # from the click to the results being ready, as the user waited for them
        elapsed = self._search_elapsed = time.time() - self._search_start_time
        text = "%s: %s in %s across %s (%s)" % (
            status, plural(len(ordered), "match", "matches"), plural(len(groups), "row"),
            plural(len(set((g.dbid, g.table) for g in groups)), "table"),
            ("%s searched, %.1fs" % (plural(total, "table"), elapsed))
            if not self._search_cancel else "%.1fs" % elapsed)
        per_db, hit_dbs = self._search_db_summary(groups)
        if hit_dbs:
            text += " · %s" % (plural(len(hit_dbs), "database") + " with matches"
                               if len(hit_dbs) > 1 else "in " + hit_dbs[0])
        details = list(per_db)
        removed = getattr(self, "_search_removed", [])
        if removed:                     # databases removed from the case since the search
            details.append("The results of %s were removed with %s" % (
                ", ".join(removed), "it" if len(removed) == 1 else "them"))
        capped = self._search_capped_text()
        if capped:
            text += " · stopped at Max rows/table in %s" % plural(
                len(getattr(self, "_search_capped", [])), "place")
            details.append(capped)
        self._search_status.set(text, details)
        # the navigator's 'Has hits' filter and the scope's 'With hits' preset
        hits = {}
        for g in groups:
            hits[g.dbid] = hits.get(g.dbid, 0) + 1
        self._navigator.set_hits(hits if self.case.multi else {})
        self._search_top.show(self._stop_btn, False)
        self._refresh_search_errors()
        self._search_progress.configure(value=total)
        self._search_progress.pack_forget()
        # Update filter combos — table filter shows per-table row counts
        tbl_labels = list(labels)
        columns = sorted(set(r["column"] for r in ordered))
        types = sorted(set(r["type"] for r in ordered))
        self._sr_table_filter.configure(values=["All"] + tbl_labels)
        self._sr_col_filter.configure(values=["All"] + columns)
        self._sr_type_filter.configure(values=["All"] + types)
        self._sr_db_filter.configure(values=["All"] + [m.name for m, _t in
                                                       getattr(self, "_search_work", [])])
        # Dynamically size combobox width to fit longest entry
        max_tbl = max((len(l) for l in tbl_labels), default=5)
        self._sr_table_filter.configure(width=max(32, min(max_tbl + 2, 55)))
        max_col = max((len(c) for c in columns), default=5)
        self._sr_col_filter.configure(width=max(24, min(max_col + 2, 45)))
        for combo in (self._sr_db_filter, self._sr_source_filter, self._sr_table_filter, self._sr_col_filter,
                      self._sr_type_filter):
            combo.set("All")
        # Auto-resize treeview Table/Column columns to fit longest name
        if not self._measure_font:
            self._measure_font = tkfont.nametofont("TkDefaultFont")
        mf = self._measure_font
        tbl_names = sorted(set(g.table for g in groups))
        if tbl_names:
            longest_tbl_px = max(mf.measure(t) for t in longest(tbl_names))
            tbl_px = max(220, min(longest_tbl_px + 30, 500))
            self._search_tree.column("Table", width=tbl_px)
        if columns:
            names = [g.columns_label() for g in groups[:2000]] \
                if self._sr_group_var.get() and groups else columns
            longest_col_px = max(mf.measure(c) for c in longest(names))
            col_px = max(170, min(longest_col_px + 30, 400))
            self._search_tree.column("Column", width=col_px)
        self._filter_search_results()

    def _search_capped_text(self):
        """Which tables stopped at 'Max rows/table' ('' when none did). The limit is the
        Search tab's own choice; All has none."""
        capped = getattr(self, "_search_capped", [])
        limit = getattr(self, "_search_limit", None)
        if not capped or limit is None:
            return ""
        names = []
        for uid, where, _n in capped:
            names.append(self.member_label(self.case.find(uid), where))
        shown = ", ".join(names[:8]) + (", and %d more" % (len(names) - 8) if len(names) > 8
                                        else "")
        return ("%d place%s stopped at %s matching rows (Max rows/table), there may be more: "
                "%s. Choose a larger Max rows/table, or All, to see every match."
                % (len(names), "" if len(names) == 1 else "s", format(limit, ","), shown))

    def _refresh_search_errors(self):
        """'N errors' shows only when a table (or source) could not be searched."""
        n = len(self._search_errors)
        self._search_err_btn.configure(text="%d error%s" % (n, "" if n == 1 else "s"))
        self._search_btn_row.show(self._search_err_btn, n > 0)

    def _search_db_summary(self, groups):
        """In a case: ([detail lines], [names of the databases with matches]): each database
        with matches and its rows, then one line for all those searched without a match
        ('22 databases: nothing found') and one for those not searched (stopped); ([], [])
        for a search of one database."""
        work = getattr(self, "_search_work", [])
        if not self.case.multi or not work:
            return [], []
        rows = {}
        for g in groups:
            rows[g.dbid] = rows.get(g.dbid, 0) + 1
        done = set(getattr(self, "_search_done_dbs", ()))
        lines, found, empty, stopped = [], [], [], []
        for m, _tables in sorted(work, key=lambda w: -rows.get(w[0].uid, 0)):
            n = rows.get(m.uid, 0)
            if n:
                lines.append("%s: %s" % (m.name, plural(n, "row")))
                found.append(m.name)
            elif m.uid in done:
                empty.append(m.name)
            else:
                stopped.append(m.name)
        if empty:
            lines.append("%s: searched, nothing found (%s)" % (
                plural(len(empty), "database"), ", ".join(empty)))
        if stopped:
            lines.append("%s: not searched (stopped) (%s)" % (
                plural(len(stopped), "database"), ", ".join(stopped)))
        return lines, found

    def _sr_filter_tests(self):
        """(group test, hit test) for the Database/Source/Table/Col/Type filters."""
        src_f = source_key(self._sr_source_filter.get())     # 'Freed pages' -> 'Freelist'
        tbl_f = self._sr_table_filter.get()
        col_f = self._sr_col_filter.get()
        type_f = self._sr_type_filter.get()
        db_f = self._sr_db_filter.get() if self._sr_db_filter.winfo_manager() else "All"
        db_uid = next((m.uid for m in self.case if m.name == db_f), None) \
            if db_f != "All" else None
        # the Table filter's entries stand for (database, source, table): "t (42)", "WAL: t",
        # "Freelist: t", and in a case "wa.db › t"
        place = getattr(self, "_sr_table_labels", {}).get(tbl_f) if tbl_f != "All" else None

        def cell_ok(r):
            return (col_f == "All" or r["column"] == col_f) and (type_f == "All" or r["type"] == type_f)

        def place_ok(table, kind, dbid):
            if src_f != "All" and kind != src_f:
                return False
            if db_uid is not None and dbid != db_uid:
                return False
            if place is None:
                return tbl_f == "All"
            pdb, psrc, ptable = place
            return table == ptable and dbid == pdb and (psrc is None or kind == psrc)

        def group_ok(g):
            return place_ok(g.table, g.source, g.dbid) and any(cell_ok(h) for h in g.hits)

        def hit_ok(r):
            return place_ok(r["table"], source_kind(r), r.get("dbid")) and cell_ok(r)
        return group_ok, hit_ok

    def result_source_label(self, g):
        """A result line's Source text: 'DB', 'WAL current ×2'...; in a case 'wa.db · DB'."""
        label = g.source_label()
        if self.case.multi and getattr(g, "database", ""):
            return "%s · %s" % (g.database, label)
        return label

    def _filter_search_results(self):
        group_ok, hit_ok = self._sr_filter_tests()
        self._sr_filtered = [r for r in self._search_results if hit_ok(r)]
        self._sr_groups_filtered = [g for g in self._sr_grouper.groups if group_ok(g)]
        self._sr_page = 0
        self._display_search_page()

    def _sr_page_items(self):
        grouped = self._sr_group_var.get()
        items = self._sr_groups_filtered if grouped else self._sr_filtered
        start = self._sr_page * self._sr_page_size
        return grouped, items, start, items[start:start + self._sr_page_size]

    def _display_search_page(self, searching=False):
        tree = self._search_tree
        tree.delete(*tree.get_children())
        self._sr_iid_map = {}
        grouped, _items, start, page = self._sr_page_items()
        term = getattr(self, '_search_term', '')
        mkey = getattr(self, '_search_mode_key', 'ci')
        headings = (("Column", "Matched in"), ("RowID", "Row"), ("Matched Value", "Preview"),
                    ("Type", "Match")) if grouped else \
            (("Column", "Column"), ("RowID", "RowID"), ("Matched Value", "Matched Value"),
             ("Type", "Type"))
        for col, text in headings:
            tree.heading(col, text=text)
        for i, item in enumerate(page):
            idx = start + i + 1
            if grouped:
                self._insert_result_group(tree, idx, i, item, term, mkey)
                continue
            r = item
            tag = self._wal_row_tag(r) or ("odd" if i % 2 else "even")
            val = _snippet(r["value"], term, mkey) if term else r["value"]
            iid = "h%d" % idx
            src = r.get("source", "DB")
            if self.case.multi and r.get("database"):
                src = "%s · %s" % (r["database"], src)
            tree.insert("", "end", iid=iid, values=(idx, src,
                                                    self._sr_table_text(r["table"], r.get("dbid")),
                                                    r["column"],
                                                    r["rowid"], val, match_label(r)), tags=(tag,))
            self._sr_iid_map[iid] = (r, None)
        self._configure_result_tags(tree)
        self.tags.refresh_search_markers()
        self._update_sr_page_label(searching)

    def _sr_table_text(self, name, dbid=None):
        """A result's table as listed: views are marked (their rows repeat table rows)."""
        if dbid is None and self.case.active is not None:
            dbid = self.case.active.uid
        return name + " (view)" if (dbid, name) in self._search_view_names else name

    def _insert_result_group(self, tree, idx, i, g, term, mkey):
        """One line per row; its matching cells and the WAL frames holding it underneath."""
        first = g.first
        tag = ("wal_" + g.category) if g.source == "WAL" and g.category in WAL_STATES else \
            ("odd" if i % 2 else "even")
        n = len(g.hits)
        preview = _snippet(first["value"], term, mkey) if term else first["value"]
        iid = "g%d" % idx
        tree.insert("", "end", iid=iid, text="",
                    values=(idx, self.result_source_label(g), self._sr_table_text(g.table, g.dbid),
                            g.columns_label(), g.rowid, preview,
                            f"{n} cells" if n > 1 else match_label(first)),
                    tags=(tag,))
        self._sr_iid_map[iid] = (first, g)
        if n > 1:
            for j, h in enumerate(g.hits):
                cid = "%s.%d" % (iid, j)
                val = _snippet(h["value"], term, mkey) if term else h["value"]
                tree.insert(iid, "end", iid=cid, values=("", "", "", h["column"], "", val, match_label(h)),
                            tags=(tag, "sr_child"))
                self._sr_iid_map[cid] = (h, g)
        if g.frames and (g.source == "DB" or len(g.frames) > 1):
            fid = iid + ".frames"
            label = "also in WAL frames" if g.source == "DB" else "WAL frames"
            tree.insert(iid, "end", iid=fid,
                        values=("", "", "", label, "", g.frames_label(), f"{len(g.frames)} frames"),
                        tags=(tag, "sr_child"))
            self._sr_iid_map[fid] = (first, g)

    def _update_sr_page_label(self, searching=False):
        grouped, items, _start, page = self._sr_page_items()
        total = len(items)
        total_pages = max(1, (total + self._sr_page_size - 1) // self._sr_page_size)
        what = "rows" if grouped else "matches"
        extra = ""
        if grouped:
            extra = f" ({sum(len(g.hits) for g in items):,} matches)"
        self._sr_page_label.configure(
            text=f"Page {self._sr_page + 1} of {total_pages}  |  Showing {len(page)} of {total:,} "
                 f"{what}{extra}" + ("  (searching...)" if searching else ""))

    def _sr_page_hits(self):
        """The hits shown on the current page (a grouped page: every hit of its rows)."""
        grouped, _items, _start, page = self._sr_page_items()
        if not grouped:
            return list(page)
        return [h for g in page for h in g.hits]

    def _sr_prev_page(self):
        if self._sr_page > 0:
            self._sr_page -= 1
            self._display_search_page()

    def _sr_next_page(self):
        _grouped, items, _start, _page = self._sr_page_items()
        total_pages = max(1, (len(items) + self._sr_page_size - 1) // self._sr_page_size)
        if self._sr_page < total_pages - 1:
            self._sr_page += 1
            self._display_search_page()

    def _stop_search(self):
        self._search_cancel = True
        th = self._search_thread
        if th is not None and th.is_alive():
            # Also stop the statement it is running: a pre-filtered scan of a large table can
            # take a long time before it yields the next row and sees the cancel flag.
            for t in [th] + list(getattr(self, "_search_threads", ())):
                self.case.interrupt(t)

    def _search_scope_names(self, member=None):
        """The tables of a database a search covers (the scope's, or all), then, with
        'Include views' on, the scope's views (all views when the scope names none). The
        views are remembered in _search_view_names as (uid, view)."""
        m = member if member is not None else self.case.active
        if m is None:
            return []
        tables, views = m.db.tables(), m.db.views()
        scope = set(m.scope_tables or ())
        names = [t for t in tables if not scope or t in scope]
        if not hasattr(self, "_search_view_names") or member is None:
            self._search_view_names = set()
        if self._search_views_var.get() and views:
            # the scope dialog chose among these views (else: every view)
            decided = m.scope_views_seen == set(views)
            picked = [v for v in views if v in scope] if decided else views
            names += picked
            self._search_view_names |= set((m.uid, v) for v in picked)
        return names

    def _show_scope(self):
        if not self.db.ok:
            return
        if self.case.multi:
            # the tables of every database searched, grouped under their database
            members = self._search_members()
            dlg = ScopeDlg.for_case(self, members)
            self.wait_window(dlg)
            if dlg.result is not None:
                for m in members:
                    m.scope_tables = dlg.result.get(m.uid, [])
                    m.scope_views_seen = set(m.db.views())
                self._search_status.configure(text=self._search_scope_text().replace(
                    "Searching", "Will search", 1).rstrip("."))
                self._update_scope_chip()
            return
        tables = self.db.tables()
        views = self.db.views()
        dlg = ScopeDlg(self, tables, self._count_cache, self._scope_tables, views)
        self.wait_window(dlg)
        if dlg.result is not None:
            self._scope_tables = dlg.result
            self.case.active.scope_views_seen = set(views)
            self._update_scope_chip()

    def _reset_scope(self):
        if self.db.ok:
            for m in self.case:
                m.scope_tables = list(m.db.tables()) + list(m.db.views())
            n = sum(len(m.scope_tables) for m in self._search_members())
            self._search_status.configure(text=f"Scope reset: {n} tables selected" + (
                " in %d databases" % len(self._search_members()) if self.case.multi else ""))
            self._update_scope_chip()

    def _result_member(self, result):
        """The database a search hit came from (the active one when it does not say)."""
        m = self.case.find(result.get("dbid")) if result.get("dbid") is not None else None
        return m if m is not None else self.case.active

    def _on_search_dblclick(self, event):
        sel = self._search_tree.selection()
        if not sel:
            return
        result, _group = self._sr_iid_map.get(sel[0], (None, None))
        if result is None:
            return
        tbl, match_col = result["table"], result["column"]
        search_term = getattr(self, '_search_term', '')
        member = self._result_member(result)
        if not is_wal(result) and source_kind(result) != "Freelist":
            loc = result.get("locator")
            if loc is not None and member is not None:
                # the row, read from its own database (the active one or not)
                RowWin.show(self, member.db, tbl, loc, search_term=search_term,
                            match_col=match_col)
            return "break"
        if member is not None:
            self.activate_member(member)    # WAL / freed-page records: that database's tabs
        if is_wal(result):
            open_wal_record(self, result, match_col, result["value"])
        else:
            self._show_record_detail(result, match_col)
        return "break"      # a double-click opens the row; the arrow expands it

    def _show_record_detail(self, hit, match_col=""):
        """A recovered record that is no longer a table row (e.g. in a freed page): the same
        recovered-record window as Forensics (values, where it was found, confidence and its
        reasons); a record known only from its hit gets a simpler window."""
        member = self._result_member(hit)
        rid = hit.get("record_id")
        if rid and member is not None:
            r = member.db.freed_record(rid)     # recovered by the search: never again here
            if r is not None:
                return RecordWindow(self._forensics, r, self)
        loc = hit.get("locator")
        snap = getattr(loc, "snapshot", None)
        cols, values = (list(snap[0]), list(snap[1])) if snap else ([], list(hit.get("row") or []))
        where = "%s page %s, cell offset %s" % (source_name(hit.get("source", "")),
                                                 hit.get("page", "?"),
                                                 hit.get("cell_offset", "?"))
        return ValuesWindow(self, "Row detail — recovered record, %s" % self.member_label(
                                member, hit["table"]),
                            "%s | table %s (confidence %s) | recorded row id %s" % (
                                where, hit["table"], hit.get("confidence", "?"),
                                hit.get("rowid", "-")), cols, values, match_col)

    def _on_search_rightclick(self, event):
        """Right-click menu on a search result: open, copy, go to the WAL frames holding it."""
        tree = self._search_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        result, group = self._sr_iid_map.get(iid, (None, None))
        if result is None:
            return
        menu = tk.Menu(self, tearoff=0)
        member = self._result_member(result)
        menu.add_command(label="Open row detail", command=lambda: self._on_search_dblclick(None))
        menu.add_command(label="Copy matched value",
                         command=lambda v=result["value"]: (self.clipboard_clear(),
                                                            self.clipboard_append(str(v))))

        def goto(fi, pn):
            self.activate_member(member)        # the frames are in that database's WAL
            self._navigate_to_wal_frame(fi, pn)
        frames = sorted(group.frames) if group is not None else []
        if not frames and result.get("frame_idx") is not None:
            frames = [(result["frame_idx"], result.get("page_num"), result.get("category", ""))]
        if len(frames) == 1:
            fi, pn, st = frames[0]
            menu.add_command(label=f"Go to WAL frame #{fi} ({st})",
                             command=lambda fi=fi, pn=pn: goto(fi, pn))
        elif frames:
            sub = tk.Menu(menu, tearoff=0)
            for fi, pn, st in frames[:40]:
                sub.add_command(label=f"Frame #{fi}  (page {pn}, {st})",
                                command=lambda fi=fi, pn=pn: goto(fi, pn))
            if len(frames) > 40:
                sub.add_command(label=f"... {len(frames) - 40} more (see the WAL tab)",
                                state="disabled")
            menu.add_cascade(label=f"Go to WAL frame ({len(frames)})", menu=sub)
        loc = result.get("locator")
        if source_kind(result) != "Freelist" and getattr(loc, "kind", None) in ("rowid", "pk") \
                and member is not None and result["table"] in member.db.tables():
            menu.add_command(label="Row history (every version)",
                             command=lambda t=result["table"], l=loc: (
                                 self.activate_member(member), self.show_row_history(t, l)))
            self.datamap.search_menu(menu, result, member)
        self.relations.search_menu(menu, result)
        if self._sr_group_var.get():
            menu.add_separator()
            menu.add_command(label="Expand all", command=lambda: self._sr_expand_all(True))
            menu.add_command(label="Collapse all", command=lambda: self._sr_expand_all(False))
        self.tags.search_menu(menu, group, result)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _sr_expand_all(self, open_):
        for iid in self._search_tree.get_children():
            self._search_tree.item(iid, open=open_)

    def _show_wal_row_detail(self, source, table, match_col, rowid, match_val,
                             row_data, frame_idx=None, page_num=None,
                             category=None, locator=None, row_values=None):
        """The row detail of a WAL record (wal_tab.WalRecordWindow): its values beside the
        database's current ones. row_values: the raw values (row_data holds display text)."""
        rec = {"table": table, "rowid": rowid, "category": category, "frame_idx": frame_idx,
               "page_num": page_num, "locator": locator, "row_data": row_data or {},
               "raw_values": list(row_values) if row_values is not None
               else list((row_data or {}).values())}
        return open_wal_record(self, rec, match_col, match_val)

    def show_wal_frame(self, frame_idx):
        """Show one WAL frame in the WAL tab (of the active database)."""
        if not self._wal_tab_added or not self.db.has_wal:
            return
        self._nb.select(self._wal_frame)
        self._wal_frame.navigate(frame_idx)

    def _navigate_to_wal_frame(self, frame_idx, page_num=None):
        self.show_wal_frame(frame_idx)

    def _show_search_errors(self):
        """Every table or source the search could not read, with why (a scrollable list)."""
        errors = list(self._search_errors)
        if not errors:
            return
        TextWindow(self, "Search errors (%d)" % len(errors),
                   "The search could not read these tables or sources; the rest were "
                   "searched:", "\n".join(errors))

    def _search_scope_choices(self):
        """[(key, text)] of what a search export can cover: every result, the results the
        filters keep, the page shown; each with its rows and matches."""
        def words(hits):
            rows = len(set(self._hit_row_key(h) for h in hits))
            return "%s row%s, %s match%s" % (format(rows, ","), "" if rows == 1 else "s",
                                             format(len(hits), ","),
                                             "" if len(hits) == 1 else "es")
        out = [("all", "All results (%s)" % words(self._search_results))]
        if len(self._sr_filtered) != len(self._search_results):
            out.append(("filtered", "Results the filters keep (%s)" % words(self._sr_filtered)))
        out.append(("page", "The page shown (%s)" % words(self._sr_page_hits())))
        return out

    @staticmethod
    def _hit_row_key(h):
        return (h.get("dbid"), h.get("source", "DB")[:3], h.get("table"),
                repr(h.get("locator")), h.get("page"), h.get("cell_offset"),
                id(h.get("frames")))

    def _search_scope_hits(self, key):
        if key == "all":
            return list(self._search_results)
        if key == "filtered":
            return list(self._sr_filtered)
        return self._sr_page_hits()

    def _search_export(self, fmt, full=False):
        """Export search results (every matching cell, with where it is) as CSV or JSON on a
        worker thread, with the search's term, mode, scope and filters in the provenance.
        full: JSON with each hit's whole row and WAL / freed-page details."""
        if not self._search_results:
            messagebox.showinfo("Export", "Search first: there are no results to export.",
                                parent=self)
            return
        scopes = self._search_scope_choices()
        opts = export_options(self, "Export search results", scopes,
                              formats=(fmt,) if full else ("csv", "json"), fmt=fmt,
                              spreadsheet_safe=True)
        if opts is None:
            return
        fmt = opts["fmt"]
        data = self._search_scope_hits(opts["scope"])
        path = ask_path(self, fmt, "search_results")
        if not write_allowed(path):
            return
        multi = self.case.multi
        cols = (["Database"] if multi else []) + [
            "#", "Source", "Table", "Column", "Row", "Value", "Type", "Page", "Cell offset",
            "Frame", "WAL frames", "Confidence"]
        if full:
            cols += ["Row values"]

        def rows_fn():
            for i, r in enumerate(data):
                loc = r.get("locator")
                row = ([self._result_member(r).path] if multi else []) + [
                    i + 1, source_name(r.get("source", "DB")), r["table"], r["column"],
                    loc if loc is not None and getattr(loc, "kind", "") != "ordinal"
                    else r.get("rowid"), r["value"], r["type"], r.get("page", r.get("page_num")),
                    r.get("cell_offset"), r.get("frame_idx"),
                    "; ".join("#%s %s" % (f[0], f[2]) for f in r.get("frames") or ()),
                    r.get("confidence")]
                if full:
                    rd = r.get("row_data")
                    row.append(dict(rd) if rd else list(r.get("row") or ()))
                yield row
        members = [m for m, _t in getattr(self, "_search_work", [])] or [self.case.active]
        mode = getattr(self, "_search_mode_key", "ci")
        filters = "; ".join("%s: %s" % (name, c.get()) for name, c in (
            ("database", self._sr_db_filter), ("source", self._sr_source_filter),
            ("table", self._sr_table_filter), ("column", self._sr_col_filter),
            ("type", self._sr_type_filter)) if c.get() != "All")
        lim = getattr(self, "_search_limit", None)
        extra = {"term": getattr(self, "_search_term", ""), "mode": mode,
                 "max_rows_per_table": lim if lim is not None else "all",
                 "stopped_at_the_limit": [self.member_label(self.case.find(u), w)
                                          for u, w, _n in getattr(self, "_search_capped", [])]}
        export_rows(self, "Export search results", path, fmt, cols, rows_fn,
                    "search results", members, scope=dict(scopes)[opts["scope"]],
                    filters=filters, blob_mode=opts["blob_mode"], total=len(data), extra=extra,
                    spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def _search_export_csv(self):
        self._search_export("csv")

    def _search_export_details(self):
        self._search_export("json", full=True)

    def _search_copy(self):
        """Copy the results (the scope chosen) as tab-separated text."""
        if not self._search_results:
            return
        scopes = self._search_scope_choices()
        opts = export_options(self, "Copy search results", scopes, formats=("text",),
                              blobs=False, ok_text="Copy")
        if opts is None:
            return
        data = self._search_scope_hits(opts["scope"])
        multi = self.case.multi
        lines = [("Database\t" if multi else "") + "#\tSource\tTable\tColumn\tRow\tValue\tType"]
        for i, r in enumerate(data):
            lines.append((r.get("database", "") + "\t" if multi else "") + "\t".join(
                str(x) for x in (i + 1, source_name(r.get("source", "DB")), r["table"],
                                 r["column"], r["rowid"], plain_text(r["value"]), r["type"])))
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self._search_status.configure(text="Copied %s results to the clipboard."
                                      % format(len(data), ","))

    # ── Browse Tab ───────────────────────────────────────────────────
    def _build_browse_tab(self):
        bf = self._browse_frame

        from breadcrumb import Breadcrumb
        top = self._browse_top = Toolbar(bf)
        top.pack(fill="x", padx=M, pady=(S, XS))
        self._browse_table_var = tk.StringVar()
        # '● msgstore.db ▾ › message ▾': the database (switch it in a case) and the table
        self._browse_crumb = top.add(Breadcrumb(top, self, table_var=self._browse_table_var))
        self._browse_table_combo = self._browse_crumb.table_combo
        self._browse_table_combo.bind("<<ComboboxSelected>>", lambda e: self._load_browse_table())

        # Global filter: every word must occur in some column; the per-column filters are in
        # the grid, under the column headers
        top.add(ttk.Label(top, text="Filter all columns:"), gap=12)
        self._browse_filter_var = tk.StringVar()
        self._browse_filter_entry = top.add(ttk.Entry(top, textvariable=self._browse_filter_var,
                                                      width=24), stretch=True, gap=2)
        add_placeholder(self._browse_filter_entry, self._browse_filter_var,
                        "words, all must match", font=F["body"])
        ToolTip(self._browse_filter_entry, "Keeps the rows where every word occurs in some "
                                           "column. Each column has its own filter under its "
                                           "header (? for the syntax).")
        self._browse_filter_var.trace_add("write", self._on_browse_filter)
        self._browse_filter_entry.bind(
            "<Return>", lambda e: self._browse_grid.set_global_filter(
                self._browse_filter_var.get(), apply=True))
        self._browse_filter_help = top.add(ttk.Button(
            top, text="?", width=2, style="Sm.TButton",
            command=lambda: show_filter_help(self._browse_filter_help)), gap=2)
        ToolTip(self._browse_filter_help, "The filter syntax: >5, 1~5, /regex/, NULL, !text, "
                                          "%like%")
        self._browse_inspector_var = tk.BooleanVar(value=False)
        self._browse_inspector_cb = top.add(ttk.Checkbutton(
            top, text="Row panel", variable=self._browse_inspector_var,
            command=lambda: self._browse_grid.set_inspector(self._browse_inspector_var.get())),
            gap=10)
        self._browse_columns_btn = top.add(ttk.Button(
            top, text="Columns…", command=lambda: self._browse_grid.column_chooser()), gap=6)
        # every named limit (rows per window, positions mapped, characters drawn, ...)
        top.add(ttk.Button(top, text="Limits…",
                           command=lambda: self.datamap.limits_window(self)))
        # one Export ▾: the rows, and the BLOBs as files only for a table that holds BLOBs
        self._browse_export_btn = top.add(ttk.Button(
            top, text="Export ▾", command=lambda: self._post_menu(
                self._browse_export_menu(), self._browse_export_btn)), gap=6)
        self._browse_blob_export = False

        # Engine notes for the rows on screen (why a table is empty or capped, columns that
        # could not be computed, flagged rows): packed above the grid when there is one.
        self._browse_note_lbl = tk.Label(bf, text="", anchor="w", justify="left",
                                         bg=K["warning_soft"], fg=C["orange"], font=F["small"],
                                         padx=8, pady=2, wraplength=1400)
        self._browse_status = ttk.Label(bf, text="", style="M.TLabel", anchor="w")
        self._browse_status.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        self._browse_grid = DataGrid(
            bf, frozen=1, on_open_row=self._on_browse_open_row,
            on_open_blob=self._on_browse_open_blob, on_sort=self._on_browse_sorted,
            on_filter=self._on_browse_filtered, on_view_change=self._on_browse_view,
            describe=self._describe_value, row_style=self.tags.browse_row_style,
            on_context_menu=self._on_browse_menu, on_header_menu=self._on_browse_header_menu)
        self._browse_grid.pack(fill="both", expand=True, padx=8, pady=(2, 2))
        # the Browse bar's 'Filter all columns' is the grid's search (not a second box)
        self._browse_grid.use_search_box(SimpleNamespace(var=self._browse_filter_var,
                                                         entry=self._browse_filter_entry))
        # saved filters per table name: kept in the settings, so a filter saved on one
        # database's 'message' table applies to another database's 'message' table too
        self._browse_grid.saved_filters = (self._load_saved_filters, self._save_saved_filter)
        self._browse_grid.filter_key = lambda: self._browse_table_var.get()
        self._browse_grid.context = lambda: "%s, %s" % (
            self._browse_table_var.get(), self.case.active.name) \
            if self.case.active is not None else ""
        # Counts of filtered rows run here, beside the grid's own window reads
        self._browse_counter = Runner(self, "browse-count", release=self._release_worker_connection)

    def _load_saved_filters(self, key):
        """{name: filters} saved for a table name (any database)."""
        store = self.tags.settings.get("saved_filters")
        per = store.get(str(key)) if isinstance(store, dict) else None
        return dict(per) if isinstance(per, dict) else {}

    def _save_saved_filter(self, key, name, filters):
        store = self.tags.settings.get("saved_filters")
        if not isinstance(store, dict):
            store = self.tags.settings["saved_filters"] = {}
        per = store.setdefault(str(key), {})
        if filters is None:
            per.pop(name, None)
        else:
            per[name] = filters
        if not per:
            store.pop(str(key), None)
        self.tags.save_settings()

    def _load_browse_table(self):
        """Show the table (or view, or 'WAL: name' WAL-only table) chosen in the combo."""
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok or tbl == "---WAL-Only Tables---":
            return
        self.tags.remember_view()       # the table shown until now keeps its widths and sort
        self._browse_count_gen += 1
        self._browse_counter.cancel()
        running = self._browse_counter.running_thread()
        if running is not None and self._browse_counter.running_key() == "blob-check":
            self.db.interrupt(running)          # the BLOB check of the table shown until now
        self._browse_count_error = ""
        self._browse_t0 = time.perf_counter()
        self._browse_first_ms = self._browse_first_range = None
        self._browse_table_gen += 1
        self._browse_wal_loading = None
        self._browse_has_blobs = False
        if tbl.startswith("WAL: "):
            # read on a worker: an empty grid with the table's columns until the records
            # arrive, and the status says what is being read
            src = ListSource(self.db.wal_browse_columns(tbl[5:]), [],
                             encoding=self.db.encoding)
            self._start_wal_only_load(tbl)
        else:
            known = self._count_cache.get(tbl)
            src = TableSource(self.db, tbl, total=known if isinstance(known, int) else None)
        self._browse_source = src
        self._browse_filter_var.set("")
        self._show_browse_blob_export(self._browse_has_blob_columns())
        self._browse_grid.set_source(src)
        self.tags.restore_view(tbl)
        self.relations.refresh_marks()      # the link glyph on columns with confident links
        self._browse_dates.apply(tbl)
        self._browse_lookups.apply(tbl)     # 'Show value from linked table' (after dates)
        self._show_browse_note([], "")
        self._start_browse_blob_check(tbl)  # queued first of all: it runs last
        self._start_browse_positions()      # queued first: the (quick) count runs before it
        self._start_browse_count()
        self._update_browse_status()

    def _start_browse_blob_check(self, tbl):
        """Export BLOBs… is offered when the table holds BLOB values: at once for a column
        declared BLOB (or with no type); otherwise a worker looks for a BLOB stored in any
        column (a TEXT or INTEGER column may hold one) and brings the button up when it finds
        one. WAL-only tables are checked when their records arrive."""
        if tbl.startswith("WAL: ") or self._browse_has_blob_columns():
            return
        db, gen = self.db, self._browse_table_gen

        def work():
            return db.has_blob_values(tbl, cancel=lambda: gen != self._browse_table_gen)

        def done(found, _error):
            if gen != self._browse_table_gen or db is not self.db or not found:
                return
            self._browse_has_blobs = True
            self._show_browse_blob_export(True)
        self._browse_counter.submit("blob-check", work, done)

    def _start_wal_only_load(self, tbl):
        """Read the records of the WAL-only table `tbl` ('WAL: name') off the Tk thread, then
        show them. A later table choice (or closing the database) drops this read."""
        db, gen = self.db, self._browse_table_gen
        self._browse_wal_loading = tbl

        def cancel():
            return gen != self._browse_table_gen or db is not self.db

        def work():
            return db.wal_browse(tbl[5:], cancel=cancel)

        def done(result, error):
            if cancel() or self._browse_table_var.get() != tbl:
                return
            if error is not None:           # the status keeps saying so (loading stays set)
                self._browse_count_error = "WAL records could not be read: %s" % error
                self._update_browse_status()
                return
            self._browse_wal_loading = None
            cols, rows, _total = result
            src = ListSource(cols, [(r, r.flags) for r in rows], encoding=db.encoding)
            self._browse_source = src
            self._browse_grid.set_source(src)
            self.tags.restore_view(tbl)
            self._browse_dates.apply(tbl)
            self._browse_lookups.apply(tbl)
            self._show_browse_blob_export(self._browse_has_blob_columns())
            self._update_browse_status()
        self._browse_counter.submit("wal-load", work, done)

    def _release_worker_connection(self):
        """Runs on a worker thread that has no more work: close its SQL connection to every
        open database (a worker may have read several of a case)."""
        self.case.release_thread_connection()

    def _start_browse_count(self):
        """Count the rows of the Browse source for its current filter, off the Tk thread."""
        src = self._browse_source
        self._browse_count_gen += 1
        gen = self._browse_count_gen
        self._browse_count_error = ""
        running = self._browse_counter.running_thread()
        if running is not None and self._browse_counter.running_key() == "count":
            self.db.interrupt(running)          # a count for an older filter: stop it
        if src is None or src.row_count() is not None:
            return

        def cancel():
            return gen != self._browse_count_gen

        def done(result, error):
            if gen != self._browse_count_gen or src is not self._browse_source:
                return
            if error is not None:
                self._browse_count_error = str(error)
            else:
                flt, n = result
                src.set_count(flt, n)
                if flt is None and isinstance(src, TableSource):
                    self._count_cache[src.table] = n
                self._browse_grid.row_count_changed()
            self._update_browse_status()
        self._browse_counter.submit("count", lambda: src.count_rows(cancel), done)

    def _start_browse_positions(self):
        """Index where the rows of the Browse view are (engine.positions), off the Tk thread,
        so windows anywhere read in milliseconds. Runs on the count worker; a build for an
        older table, sort or filter is stopped."""
        src = self._browse_source
        self._browse_pos_gen += 1
        gen = self._browse_pos_gen
        running = self._browse_counter.running_thread()
        if running is not None and self._browse_counter.running_key() == "positions":
            # the database the build reads (in a case, maybe not the active one any more)
            (self._browse_pos_db or self.db).interrupt(running)
        self._browse_pos_busy = False
        if src is None or not hasattr(src, "build_positions"):
            return
        self._browse_pos_busy = True
        self._browse_pos_db = src.db    # each database of a case keeps its own indexes

        def cancel():
            return gen != self._browse_pos_gen

        def done(_result, error):
            if gen != self._browse_pos_gen:
                return
            self._browse_pos_busy = False     # an error only leaves windows read as before
            if error is None and self._browse_grid.waiting():
                # a jump far into the view is still read the slow way: read it again
                # through the index just built
                self._browse_grid.restart_reads()
            self._update_browse_status()
        self._browse_counter.submit("positions", lambda: src.build_positions(cancel), done)

    def _show_browse_note(self, rows, note="", keep_space=False):
        """Show the engine's note and a count of flagged rows among `rows` (objects with a
        .flags set); hidden when there is none. keep_space: while scrolling, a note that goes
        away leaves its line empty instead of making the grid jump."""
        text = " | ".join(t for t in (note, flag_summary(rows)) if t)
        lbl = self._browse_note_lbl
        if text:
            lbl.configure(text="⚠ " + text, bg=K["warning_soft"])
            if not lbl.winfo_manager():
                lbl.pack(fill="x", padx=8, pady=(0, 2), before=self._browse_grid)
        elif lbl.winfo_manager():
            if keep_space:
                lbl.configure(text="", bg=C["bg"])
            else:
                lbl.pack_forget()

    def _on_browse_view(self, grid):
        """The grid shows other rows or new data: refresh the note and the status line."""
        src = self._browse_source
        if src is None:
            return
        rows = [SimpleNamespace(flags=flags) for _values, flags in grid.visible_rows_data()]
        note = src.note
        if grid.load_error:
            note = "; ".join(t for t in (note, "rows could not be read: " + grid.load_error) if t)
        self._show_browse_note(rows, note, keep_space=True)
        if self._browse_first_ms is None and self._browse_t0 is not None \
                and not self._browse_wal_loading \
                and (rows or grid.row_count_exact()) and not grid.loading():
            self._browse_first_ms = (time.perf_counter() - self._browse_t0) * 1000
            self._browse_first_range = grid.target_row_range()
        elif self._browse_first_ms is not None \
                and grid.target_row_range() != self._browse_first_range:
            self._browse_forget_first_time()       # it timed other rows: no longer said
        self._update_browse_status()

    def _schedule_browse_status(self):
        """While rows are being read, the status line says so again each second."""
        if getattr(self, "_browse_status_after", None) is None:
            def tick():
                self._browse_status_after = None
                self._update_browse_status()
            self._browse_status_after = self.after(1000, tick)

    def _browse_forget_first_time(self):
        """The 'opened in N ms' time describes the first rows of the table as opened; once
        the view scrolls, sorts or filters it is left out."""
        self._browse_t0 = self._browse_first_ms = self._browse_first_range = None

    def _on_browse_filtered(self, _col_exprs, _global_text):
        self._browse_forget_first_time()
        self._start_browse_positions()
        self._start_browse_count()
        self._update_browse_status()

    def _on_browse_sorted(self, _column, _desc):
        self._browse_forget_first_time()
        self._start_browse_positions()
        self._update_browse_status()

    def _on_browse_filter(self, *args):
        self._browse_grid.set_global_filter(self._browse_filter_var.get())

    def _browse_clear_filters(self):
        self._browse_filter_var.set("")
        self._browse_grid.clear_filters()

    def _update_browse_status(self):
        src, g = self._browse_source, self._browse_grid
        if src is None:
            self._browse_status.configure(text="")
            return
        parts = []
        if self.case.multi:             # which database the rows are read from
            parts.append(self.member_label(self.case.active, self._browse_table_var.get()))
        n = src.row_count()
        if self._browse_wal_loading:
            parts.append("reading the WAL records of %s…" % self._browse_wal_loading[5:]
                         if not self._browse_count_error else self._browse_count_error)
            n = -1
        if n == -1:
            pass
        elif n is None:
            approx = self._count_cache.get(getattr(src, "table", None))
            about = (" (about %s)" % fmt_count(approx)[1:]) \
                if isinstance(approx, str) and approx.startswith("~") and not src.filtered else ""
            parts.append("counting rows…" + about if not self._browse_count_error
                         else "row count failed: %s" % self._browse_count_error)
        else:
            text = "%s rows" % format(n, ",")
            if src.filtered:
                text += (" (filtered from %s)" % format(src.total, ",")) \
                    if src.total is not None else " (filtered)"
            parts.append(text)
        first, end = g.target_row_range()
        if end > first:
            if g.waiting():
                # rows far into a sorted or filtered view: say what is read and for how long
                secs = g.waiting_seconds()
                parts.append("reading rows %s–%s ⟳%s" % (
                    format(first + 1, ","), format(end, ","),
                    (" %d s%s" % (secs, ", faster once the rows are indexed"
                                  if self._browse_pos_busy else "")) if secs >= 1 else ""))
                self._schedule_browse_status()
            else:
                parts.append("showing %s–%s" % (format(first + 1, ","), format(end, ",")))
        if self._browse_pos_busy:
            parts.append("indexing rows for fast scrolling…")
        if g.notice:
            parts.append(g.notice)
        total_cols = len(g.columns()) - 1
        hidden = len(g.hidden_columns())
        parts.append("%d columns%s" % (total_cols, (" (%d hidden)" % hidden) if hidden else ""))
        name, desc = g.sort_state()
        if name:
            parts.append("sorted by %s %s" % ("row order" if name == "_rid" else name,
                                              "▼" if desc else "▲"))
        bad = g.filter_errors()
        if bad:
            parts.append("filter not used for %s (hover it for why)" % ", ".join(sorted(bad)))
        if isinstance(src, ListSource):
            parts.append("WAL-only table")
        if self._browse_lookups.last_note:     # a saved lookup that cannot be shown now
            parts.append("linked values not shown: " + self._browse_lookups.last_note)
        if self._browse_first_ms is not None:     # only while the rows it timed are shown
            parts.append("opened in %s ms" % format(int(self._browse_first_ms), ","))
        self._browse_status.configure(text="  |  ".join(parts))

    @staticmethod
    def _describe_value(v):
        """Timestamp readings of a number, for the row inspector's Decoded column (the one
        decoder, engine.decode.timestamps)."""
        found = timestamps.readings(v)
        return "; ".join("%s: %s" % (label, when) for _k, label, when in found[:2])

    def _on_browse_open_row(self, _row, values):
        tbl = self._browse_table_var.get()
        if not values:
            return
        loc = values[0]
        # WAL-only tables: the record from its own frame (the row says which)
        if tbl.startswith("WAL: ") and self.db.has_wal:
            cols = self._browse_source.columns() if self._browse_source is not None else []
            frame = values[cols.index("_wal_frame")] if "_wal_frame" in cols else None
            for rec in (self.db.wal.records_of_frame(frame) if isinstance(frame, int) else ()):
                if rec["locator"] == loc:
                    open_wal_record(self, rec)
                    return
        else:
            RowWin.show(self, self.db, tbl, loc)

    def _on_browse_open_blob(self, row, column, value):
        data = self._browse_grid.row_data(row)
        where = " row %s" % data[0][0] if data and data[0] else ""
        BlobViewer(self, value, column, "%s.%s%s" % (
            self.member_label(self.case.active, self._browse_table_var.get()), column, where))

    def _on_browse_menu(self, menu, row, col):
        """Browse grid right-click: the row's history, then the Tag items."""
        tbl = self._browse_table_var.get()
        data = self._browse_grid.row_data(row)
        loc = data[0][0] if data and data[0] else None
        if getattr(loc, "kind", None) in ("rowid", "pk") and tbl in self.db.tables():
            menu.add_separator()
            menu.add_command(label="Row history (every version)",
                             command=lambda: self.show_row_history(tbl, loc))
            self.datamap.browse_menu(menu, tbl, row)
        self.relations.browse_menu(menu, tbl, data, col)
        self.tags.browse_menu(menu, row, col)

    def _on_browse_header_menu(self, menu, c):
        self._browse_dates.header_menu(menu, c)
        self._browse_lookups.header_menu(menu, c)
        self.relations.header_menu(menu, self._browse_table_var.get(), c)

    # ── Column relationships (relations_view) ────────────────────────
    def show_column_relations(self, table, column):
        """Open the map of the columns related to table.column."""
        return self.relations.column_map(table, column) if self.db.ok else None

    def browse_table(self, table):
        """Show a table of the active database in Browse."""
        self._nb.select(self._browse_frame)
        self._browse_table_var.set(table)
        self._load_browse_table()

    def browse_related(self, table, column, value):
        """Show the rows of table whose column equals value in Browse (a filter on the
        column; for the rowid, the row itself in its row detail)."""
        self.browse_table(table)
        if column is ROWID:
            RowWin.show(self, self.db, table, Locator("rowid", value))
            return
        expr = value_expr(value)
        cols = self._browse_grid.columns()
        if expr is None or column not in cols:
            return
        c = cols.index(column)
        if c in self._browse_grid.hidden_columns():
            self._browse_grid.show_column(c)
        self._browse_grid.set_filter_text(c, expr)

    def open_blob(self, value, column, context=""):
        """Open the BLOB inspector for a value from any tab."""
        BlobViewer(self, bytes(value), column, context)

    def show_row_history(self, table, key):
        """Show every version of a row (main file, WAL frames, journal) in the Forensics tab."""
        self._nb.select(self._forensics)
        self._forensics.show_history(table, key)

    def _browse_label(self):
        """'table' (or 'db › table' in a case) of the Browse view, for titles and exports."""
        return self.member_label(self.case.active, self._browse_table_var.get())

    def _browse_filters_text(self):
        """The Browse filters in words (for an export's provenance), '' when none."""
        g = self._browse_grid
        parts = ["%s: %s" % kv for kv in sorted(g.filter_texts().items())]
        glob = self._browse_filter_var.get().strip()
        if glob:
            parts.insert(0, "words: %s" % glob)
        name, desc = g.sort_state()
        if name:
            parts.append("sorted by %s %s" % ("row order" if name == "_rid" else name,
                                              "descending" if desc else "ascending"))
        return "; ".join(parts)

    def _browse_export_menu(self):
        """Browse › Export ▾: the rows (CSV or JSON) and, when the table holds BLOBs, the
        BLOBs as files."""
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Rows (CSV or JSON)…", command=self._browse_export)
        m.add_command(label="Tables (CSV)…", command=self._browse_export_tables)
        if self._browse_blob_export:
            m.add_command(label="BLOBs as files…", command=self._browse_export_blobs)
        return m

    def _show_browse_blob_export(self, on):
        self._browse_blob_export = bool(on)

    def _browse_has_blob_columns(self):
        """True when the Browse table has BLOBs to export: a column declared BLOB (or with no
        type), BLOB values found in another column (_start_browse_blob_check), or for a
        WAL-only table, BLOB values among its records."""
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return False
        if self._browse_has_blobs:
            return True
        if tbl.startswith("WAL: "):
            src = self._browse_source
            return src is not None and any(
                isinstance(v, (bytes, bytearray)) for row in src.iter_all() for v in row[1:])
        try:
            types = [(t or "").upper() for _n, t in self.db.columns(tbl)]
        except Exception:               # noqa: BLE001 - a view or odd table: offer it
            return True
        return any(t == "" or "BLOB" in t for t in types)

    def _browse_export(self, fmt=None):
        """Export the rows passing the Browse filters (all of them, in the grid's order, not
        only those on screen) or the selected rows, to CSV or JSON, on a worker thread with
        progress and Stop, with the provenance (evidence SHA-256, filters) in a manifest."""
        src, g = self._browse_source, self._browse_grid
        tbl = self._browse_table_var.get()
        if src is None or not self.db.ok:
            return
        n = src.row_count()
        count = format(n, ",") if n is not None else "counting…"
        scopes = [("rows", ("Rows the filters keep (%s)" if src.filtered else "All rows (%s)")
                   % count)]
        sel = g.selected_rows()
        if sel is not None and sel[1] > sel[0]:
            scopes.append(("selected", "Selected rows (%s)" % format(sel[1] - sel[0] + 1, ",")))
        opts = export_options(self, "Export %s" % self._browse_label(), scopes, fmt=fmt,
                              hidden=bool(g.hidden_columns()), spreadsheet_safe=True,
                              note="Values are written as stored (a column shown as a date "
                                   "or a linked value keeps its raw value; the linked value "
                                   "gets a column of its own).")
        if opts is None:
            return
        fmt, which = opts["fmt"], opts["scope"]
        real_name = tbl[5:] if tbl.startswith("WAL: ") else tbl
        path = ask_path(self, fmt, safe_filename(real_name))
        if not write_allowed(path):
            return
        cols = src.columns()
        keep = [i for i in range(len(cols))
                if not (opts["skip_hidden"] and i in g.hidden_columns())]
        names = [cols[i] for i in keep]
        # a column shown with a linked value keeps its raw values; the looked-up ones go in
        # a column of their own, named '<column> → <db › table.column>'
        extra = self._browse_lookups.export_columns(names)
        raw_n = len(names)
        names = names + [nm for _i, nm, _lk in extra]
        win = limits.get("grid_window_rows")

        def rows_fn():
            if which == "selected":
                lo, hi = sel

                def gen():
                    r = lo
                    while r <= hi:
                        got = src.rows(r, min(win, hi - r + 1))
                        if not got:
                            return
                        for values, _flags in got:
                            yield values
                        r += len(got)
                source_rows = gen()
            else:
                source_rows = src.iter_rows()
            for row in source_rows:
                vals = [row[i] if i < len(row) else None for i in keep]
                for i, _nm, lk in extra:
                    hit = lk.value(vals[i]) if i < raw_n else None
                    vals.append(hit[0] if hit is not None else None)
                yield vals
        total = (sel[1] - sel[0] + 1) if which == "selected" else n
        source = "Browse %s '%s' (%s)" % (
            "WAL-only table" if tbl.startswith("WAL: ") else "table", real_name,
            "rows from WAL frames" if tbl.startswith("WAL: ") else
            ("DB, WAL applied" if self.db.has_wal and self.db.session.sql_sees_current_state
             else "DB"))
        export_rows(self, "Export %s" % self._browse_label(), path, fmt, names, rows_fn,
                    source, [self.case.active], scope=dict(scopes).get(which, which),
                    filters=self._browse_filters_text(), blob_mode=opts["blob_mode"],
                    total=total, spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def _browse_export_tables(self):
        """Every ticked table as its own CSV in a folder (delimiter, encoding and BLOB
        handling chosen up front), on a worker thread with progress and Stop; each table
        gets its own manifest next to its CSV."""
        if not self.db.ok:
            return
        session = self.db.session
        tables = session.tables()
        if not tables:
            self.status("No tables to export.")
            return
        # the quick estimate (max rowid; an exact count already made is used): a COUNT(*) of
        # every table here would freeze the window on a big database
        counts = []
        for t in tables:
            known = self._count_cache.get(t)
            n = known if isinstance(known, int) else self.db.approx_count(t)
            counts.append((t, n))
        opts = multi_table_options(self, counts)
        if opts is None:
            return
        from tkinter import filedialog
        folder = filedialog.askdirectory(parent=self, title="Folder for the exported tables",
                                         mustexist=False)
        if not folder:
            return
        from utils import safe_filename
        picked = opts["tables"]
        delim, enc, blob_mode = opts["delimiter"], opts["encoding"], opts["blob_mode"]
        safe = opts["spreadsheet_safe"]

        def work(job):
            job.status = "Reading the evidence hashes\u2026"
            records = evidence_records([self.case.active], lambda: job.cancelled,
                                            lambda t: setattr(job, "status", t))
            if job.cancelled:
                return None
            written = []
            used_paths = set()
            for ti, t in enumerate(picked):
                if job.cancelled:
                    break
                job.status = "Table %d of %d: %s\u2026" % (ti + 1, len(picked), t)
                # read through the engine like Browse (native reads, Safe parse, WAL-merged
                # images all work; a bare SQL query would not)
                src = TableSource(self.db, t)
                cols = src.columns()
                base = safe_filename(t)
                path = os.path.join(folder, base + ".csv")
                n = 2
                # Two tables can sanitize to the same file name (a/b and a_b);
                # never let the second silently overwrite the first.
                while path in used_paths or os.path.exists(path):
                    path = os.path.join(folder, "%s_%d.csv" % (base, n))
                    n += 1
                used_paths.add(path)
                info = ex.provenance(VERSION, records, "Browse table '%s'" % t,
                                     "all rows", "", cols, blob_mode=blob_mode,
                                     spreadsheet_safe=safe)

                def rows_fn(src=src):
                    for row in src.iter_all():
                        yield list(row)

                def progress(n, t=t):
                    job.done = n
                    job.status = "%s: %s rows" % (t, format(n, ","))

                rr = ex.write_rows(path, "csv", cols, rows_fn(), info, blob_mode,
                                   lambda: job.cancelled, progress,
                                   export_protected(self),
                                   delimiter=delim, encoding=enc)
                written.append((t, rr))
            return written

        def done(result, error, cancelled):
            if error is not None:
                self.status("Export failed: %s" % error)
                return
            if result is None:
                return
            if cancelled:
                # Stop was pressed: the tables finished so far are complete, but the
                # export as a whole is not. Say so instead of reporting success.
                self.status("STOPPED: exported %d of %d table(s) to %s" %
                            (len(result), len(picked), folder))
                self.activity("export_tables", tables=[t for t, _r in result], folder=folder,
                              delimiter=delim, encoding=enc, complete=False)
                return
            names = ", ".join("%s.csv" % safe_filename(t) for t, _r in result)
            self.status("Exported %d table(s) to %s: %s" % (len(result), folder, names))
            self.activity("export_tables", tables=[t for t, _r in result], folder=folder,
                          delimiter=delim, encoding=enc)

        Job(self, "Export tables", work, done, unit="rows",
            members=[self.case.active],
            release=getattr(self, "_release_worker_connection", None))

    BLOB_KIND_ROWS = 200        # rows looked at to decide which decoded forms to offer

    def _browse_blob_kinds(self, src):
        """({kind: count}, rows looked at) of the BLOBs in the first BLOB_KIND_ROWS rows of
        the Browse table (engine.decode.render.blob_kind)."""
        import itertools
        from collections import Counter
        from engine.decode.render import blob_kind
        counts, looked = Counter(), 0
        rows = None
        try:
            rows = src.iter_all()
            for row in itertools.islice(rows, self.BLOB_KIND_ROWS):
                looked += 1
                for v in row[1:]:
                    if isinstance(v, (bytes, bytearray)) and v:
                        try:
                            counts[blob_kind(bytes(v))] += 1
                        except Exception:       # noqa: BLE001 - counted as not decoded
                            counts["raw"] += 1
        except Exception:                       # noqa: BLE001 - offer the plain export
            pass
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:               # noqa: BLE001
                    pass
        return counts, looked

    def _browse_export_blobs(self):
        """Every BLOB of the rows the filters keep (or of every row) as files in a folder, on
        a worker thread with progress and Stop, with a manifest (evidence and file hashes)."""
        src = self._browse_source
        tbl = self._browse_table_var.get()
        if src is None or not self.db.ok:
            return
        real_name = tbl[5:] if tbl.startswith("WAL: ") else tbl
        n = src.row_count()
        total = src.total
        scopes = []
        if src.filtered:
            scopes.append(("rows", "Rows the filters keep (%s)" % (
                format(n, ",") if n is not None else "counting…")))
        scopes.append(("all", "All rows in the table (%s)" % (
            format(total, ",") if total is not None else "counting…")))
        # the decoded forms are offered only when the table holds BLOBs they apply to (the
        # first rows are looked at, and the dialog says how many and what was found)
        counts, looked = self._browse_blob_kinds(src)
        formats = ["files"]
        if any(counts.get(k) for k in ("plist", "protobuf", "json", "other")):
            formats.append("decoded_json")
        if counts.get("plist"):
            formats.append("plist_xml")
        from engine.decode.render import kinds_text
        found = "In the first %s rows: %s." % (format(looked, ","), kinds_text(counts))
        if len(formats) == 1:
            found += " Nothing there decodes, so the BLOBs are saved as they are stored."
        elif "plist_xml" not in formats:
            found += " No plist there, so XML property lists are not offered."
        opts = export_options(self, "Export BLOBs of %s" % self._browse_label(), scopes,
                              formats=tuple(formats), blobs=False,
                              extra_check=("Also save each BLOB's original bytes beside it"
                                           if len(formats) > 1 else None),
                              extra_for=("decoded_json", "plist_xml"),
                              note=found + "\n\nEach BLOB becomes a file named after its "
                                   "table, row and column, in the folder you choose; "
                                   "existing files are never replaced. A manifest with every "
                                   "file's SHA-256 is written in the folder.")
        if opts is None:
            return
        folder = filedialog.askdirectory(title="Choose a folder for the BLOBs", parent=self)
        if not write_allowed(folder):
            return
        cols = src.columns()
        rows_all = opts["scope"] == "all"
        expected = total if rows_all else n
        member = self.case.active
        mode = {"decoded_json": "json", "plist_xml": "xml"}.get(opts["fmt"], "raw")
        keep_raw = mode != "raw" and opts.get("extra")
        how = {"raw": "as stored", "json": "decoded as JSON",
               "xml": "plists as XML property lists"}[mode]
        if keep_raw:
            how += ", with the original bytes"
        skipped = {}

        def work(job):
            files = []
            rows = src.iter_all() if rows_all else src.iter_rows()

            def progress(scanned, written):
                job.done = scanned
                job.status = "%s BLOBs written (%s of %s rows scanned)" % (
                    format(written, ","), format(scanned, ","),
                    format(expected, ",") if expected is not None else "?")
            count, errors, first = export_row_blobs(folder, real_name, cols, rows, progress,
                                                    lambda: job.cancelled, files, mode=mode,
                                                    keep_raw=keep_raw, skipped=skipped)
            job.status = "Writing the manifest…"
            manifest = write_export_manifest(self, folder, "BLOBs of Browse table '%s' (%s)"
                                           % (real_name, how), [member], files,
                                           not job.cancelled, dict(scopes).get(opts["scope"]))
            return count, errors, first, manifest

        def done(result, error, cancelled):
            left = sum(skipped.values())
            note = "" if not left else " (%s BLOB%s left out: not a plist)" % (
                format(left, ","), "" if left == 1 else "s")
            blob_export_done(self, "BLOBs of %s, %s%s" % (real_name, how, note), folder,
                             result, error, cancelled)
        Job(self, "Export BLOBs of %s" % self._browse_label(), work, done, total=expected,
            release=self._release_worker_connection, members=[member])

    # ── Key bindings ─────────────────────────────────────────────────
    def _on_resize(self, event):
        if event.widget is not self:
            return
        if event.width < 200:
            return                      # the window is not laid out yet
        narrow = event.width < 1100
        # crossing below 1000 px folds the navigator to its ▶ strip (crossing back brings it
        # back unless it was hidden by hand); a panel opened by hand in a narrow window stays
        wide = event.width >= NAV_BREAKPOINT
        was = getattr(self, "_nav_wide", None)
        self._nav_wide = wide
        if was is not wide:
            if not wide and self._sidebar_visible:
                self._set_sidebar(False)
                self._nav_auto_hidden = True
            elif wide and not self._sidebar_visible and getattr(self, "_nav_auto_hidden", False):
                self._nav_auto_hidden = False
                self._set_sidebar(True)
        if self._sidebar_visible:
            want = self._saved_nav_width() or self._default_nav_width(narrow)
            try:
                pos = int(self._body.sashpos(0))
            except (tk.TclError, ValueError):
                pos = 0
            if getattr(self, "_nav_width", None) != want or pos < 40:
                self._place_nav_sash(want)
        if narrow and self._title_lbl.winfo_manager():
            self._title_lbl.pack_forget()
        elif not narrow and not self._title_lbl.winfo_manager():
            self._title_lbl.pack(side="left", padx=(S, 0), after=self._logo)

    def _saved_nav_width(self):
        """The navigator width the user dragged the splitter to (kept in the settings)."""
        v = self.tags.settings.get("navigator_width")
        return v if isinstance(v, int) and 160 <= v <= 900 else None

    def _nav_sash_moved(self, _e=None):
        if not self._sidebar_visible:
            return
        try:
            pos = int(self._body.sashpos(0))
        except (tk.TclError, ValueError):
            return
        if 160 <= pos <= 900 and pos != self.tags.settings.get("navigator_width"):
            self.tags.settings["navigator_width"] = pos
            self._nav_width = pos
            self.tags.save_settings()

    def _default_nav_width(self, narrow):
        """The navigator's width when the user never dragged it: 240 / 280 px at 100%,
        grown with the display scaling (a 200% screen needs twice the pixels)."""
        try:
            f = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72.0))
        except (tk.TclError, ValueError):
            f = 1.0
        return int((240 if narrow else 280) * f)

    def _place_nav_sash(self, want):
        """Put the splitter at `want` px once the panes are laid out: a sash moved before
        that is clamped to 0 by Tk, and the panel would stay 0 px wide (unmapped while the
        state says shown)."""
        self._nav_sash_want = want
        if getattr(self, "_nav_sash_after", None) is None:
            self._place_nav_sash_now(20)

    def _place_nav_sash_now(self, tries):
        self._nav_sash_after = None
        want = self._nav_sash_want
        if not self._sidebar_visible:
            return
        width = self._body.winfo_width()
        if width < want + 150 and tries > 0:
            self._nav_sash_after = self.after(50, lambda: self._place_nav_sash_now(tries - 1))
            return
        try:
            self._body.sashpos(0, min(want, max(width - 150, 120)))
        except tk.TclError:
            return
        self._nav_width = want

    def _toggle_sidebar_by_hand(self):
        """☰, the ◀/▶ strip and Ctrl+B: the choice is kept for the next start."""
        self._nav_auto_hidden = False
        self._toggle_sidebar()
        if self.tags.settings.get("navigator_hidden") != (not self._sidebar_visible):
            self.tags.settings["navigator_hidden"] = not self._sidebar_visible
            self.tags.save_settings()

    def _toggle_sidebar(self):
        """Hide or show the Case navigator (Ctrl+B)."""
        self._set_sidebar(not self._sidebar_visible)

    def _set_sidebar(self, show):
        """Show or hide the navigator; the state, the strip, ☰ and Ctrl+B always agree with
        what the window shows."""
        shown = str(self._navigator) in [str(p) for p in self._body.panes()]
        if show and not shown:
            self._body.insert(0, self._navigator, weight=0)
        elif not show and shown:
            self._body.forget(self._navigator)
        self._sidebar_visible = bool(show)
        if show:
            self._nav_width = None
            self._place_nav_sash(self._saved_nav_width() or self._default_nav_width(
                self.winfo_width() < 1100))
        self._update_nav_rail()

    def _update_nav_rail(self):
        """The strip at the navigator's edge (◀ while the panel shows, ▶ at the window's edge
        when it is hidden) and the ☰ button say what a click does."""
        rail = getattr(self, "_nav_rail", None)
        if rail is None:
            return
        shown = self._sidebar_visible
        tip = ("Hide the databases panel (Ctrl+B)" if shown
               else "Show the databases panel (Ctrl+B)")
        rail.configure(text="◀" if shown else "▶")
        self._nav_rail_tip.text = tip
        nav_tip = getattr(self, "_nav_btn_tip", None)
        if nav_tip is not None:
            nav_tip.text = tip

    def open_palette(self, _event=None):
        """Ctrl+K: the command palette over every database, table, column, tab and action."""
        if self._palette is not None and self._palette.winfo_exists():
            self._palette.focus_force()
            return "break"
        settings = self.tags.settings
        recent = [tuple(k) for k in settings.get("palette_recent") or () if isinstance(k, list)]

        def chosen(item):
            keys = [list(item.key)] + [list(k) for k in recent if tuple(k) != item.key]
            settings["palette_recent"] = keys[:limits.get("palette_recent")]
            self.tags.save_settings()
        self._palette = CommandPalette(self, self.palette_items(), recent, chosen)
        return "break"

    def palette_items(self):
        """Everything the command palette offers."""
        items = []
        nav = self._navigator
        index = nav.index if nav.index is not None else None
        multi = self.case.multi
        if index is not None:
            for e in index.entries:
                m = e.member
                if e.kind == "database":
                    items.append(Item("database", m.name, m.path, ("database", m.path),
                                      lambda m=m: (self.activate_member(m),
                                                   nav.select_member(m))))
                elif e.kind == "table":
                    items.append(Item("table", e.table, m.name if multi else "",
                                      ("table", m.path, e.table),
                                      lambda m=m, t=e.table: self.browse_member_table(m, t)))
                else:
                    items.append(Item("column", e.column, "%s%s" % (
                        m.name + " \u203A " if multi else "", e.table),
                        ("column", m.path, e.table, e.column),
                        lambda m=m, t=e.table: self.browse_member_table(m, t)))
        for tab in self._nb.tabs():
            title = self._nb.tab(tab, "text").strip()
            items.append(Item("tab", title, "", ("tab", title),
                              lambda tab=tab: self._nb.select(tab)))
        ok = self.db.ok
        actions = [("Open database\u2026", self._open_file, True),
                   ("Open folder\u2026", self._open_folder, True),
                   ("Add database(s)\u2026", self._add_databases, ok),
                   ("Build timeline", self._palette_build_timeline, ok),
                   ("Find value\u2026 (search every table)", self._focus_search, ok),
                   ("Export Database Map\u2026", lambda: self.datamap.export_map(), ok),
                   ("Schema report (HTML)\u2026", self._schema_export_html, ok),
                   ("Evidence and verification\u2026", self._show_evidence, ok),
                   ("Issues\u2026", self._show_issues, ok),
                   ("Activity log\u2026", self._show_activity, ok),
                   ("Limits\u2026", lambda: self.datamap.limits_window(self), True),
                   ("Toggle the navigator (Ctrl+B)", self._toggle_sidebar_by_hand, True),
                   ("Help", lambda: HelpDialog(self), True),
                   ("Close", self._close_db, ok)]
        for label, fn, enabled in actions:
            if enabled:
                items.append(Item("action", label, "", ("action", label), fn))
        return items

    def _palette_build_timeline(self):
        self._nb.select(self._timeline)
        self._timeline.build_when_ready()

    def _bind_keys(self):
        self.bind("<Control-o>", lambda e: self._open_file())
        self.bind_all("<Control-k>", self.open_palette)
        self.bind_all("<Control-K>", self.open_palette)
        self.bind("<Control-b>", lambda e: (self._toggle_sidebar_by_hand(), "break")[1])
        # Ctrl+F: the search field of the window or tab in view (the Search tab otherwise)
        self.bind_all("<Control-f>", self._ctrl_f)
        # Escape stops a search only from the Search tab's own controls (an Escape in a Browse
        # filter clears that filter and must not stop a search running meanwhile)
        for w in (self._search_entry, self._mode_combo, self._search_tree, self._search_btn,
                  self._stop_btn):
            w.bind("<Escape>", lambda e: self._stop_search(), add="+")
        self._search_entry.bind("<Return>", lambda e: self._do_search())
        self.bind("<Control-e>", lambda e: self._focus_sql_editor())

    def _setup_tooltips(self):
        """Add tooltips to all interactive widgets."""
        # Search tab
        ToolTip(self._search_entry, "Type your search term and press Enter")
        ToolTip(self._mode_combo, "How to match, in three groups: text (%s), binary (%s) and "
                                  "schema (%s)." % tuple(
                                      ", ".join(k for k, v in SEARCH_MODES.items() if v in keys)
                                      for _g, keys in SEARCH_MODE_GROUPS))
        ToolTip(self._search_adv_btn,
                "Include BLOB bytes: also search inside BLOB values as bytes (the text as\n"
                "UTF-8, UTF-16LE and UTF-16BE, and in 'Text in BLOBs' mode as a hex pattern).\n"
                "Include decoded BLOBs: property lists, keyed archives, protobuf, typedstream,\n"
                "JSON, base64 and compressed data (gzip, zlib...). Both are slower.\n"
                "Include views: off by default, a view usually repeats rows of its tables.\n"
                "Max matching rows per table: a table that reaches it is named in the status\n"
                "('urls (100+)' in the Table filter). All: no limit.")
        ToolTip(self._search_free_cb, "Also search deleted records still held by freed pages\n"
                                      "(Forensics > Freed Pages shows them page by page)")
        ToolTip(self._search_wal_cb,
                "Also search every row version kept in the WAL (Write-Ahead Log) file:\n"
                "older versions, deleted rows, uncommitted and stale frames.\n"
                "Results are coloured by frame state:\n" + "\n".join(
                    "  %s: %s" % (v[0], v[3]) for v in WAL_STATES.values()))
        ToolTip(self._search_btn, "Search the tables in the scope (Enter); the line under the "
                                  "results says where it looked")
        ToolTip(self._open_btn, "Open database…, Open folder… (tick the SQLite files of a "
                                "folder), Add database(s)… (they join the case), Recent")
        ToolTip(self._palette_btn, "Find any database, table, column, tab or action (Ctrl+K)")
        self._nav_btn_tip = ToolTip(self._nav_btn, "Hide the databases panel (Ctrl+B)")
        self._update_nav_rail()
        ToolTip(self._stop_btn, "Stop the running search (Escape)")
        ToolTip(self._reset_scope_btn, "Search every table again")
        ToolTip(self._sr_table_filter, "Filter results by table (shows result count per table)")
        ToolTip(self._sr_col_filter, "Filter results by column name")
        ToolTip(self._search_tree, "Double-click or press Enter to open row detail")
        # Browse tab
        ToolTip(self._browse_table_combo, "Select a table to browse")
        ToolTip(self._browse_filter_entry,
                "Keep rows where every word occurs in some column (any case);\n"
                "\"quoted words\" count as one. Each column also has its own filter\n"
                "under its header: >5, =x, 5~10, !text, a%b, /regex/, NULL ...")
        ToolTip(self._browse_inspector_cb,
                "Show every column of the current row on the right.\n"
                "In the grid: double-click or Enter opens the row detail;\n"
                "right-click copies rows, filters by a value or hides columns.")
        ToolTip(self._browse_columns_btn, "Choose which columns the grid shows")

        if hasattr(self, "_sql_run_btn"):
            ToolTip(self._sql_run_btn,
                    "Run the SQL query (Ctrl+Enter)\n"
                    "Statements that read: SELECT, WITH, VALUES, EXPLAIN, read-only PRAGMA")
            ToolTip(self._sql_cancel_btn, "Stop the running query")
            ToolTip(self._sql_editor,
                    "Ctrl+Enter: run  |  Alt+↑/↓: history  |  Ctrl+E: go to the editor")

    def _ctrl_f(self, event=None):
        """Ctrl+F in a window focuses its search field; in the main window the one of the tab
        shown, or the Search tab when that tab has none."""
        try:
            top = event.widget.winfo_toplevel() if event is not None and \
                hasattr(event.widget, "winfo_toplevel") else self
        except (tk.TclError, KeyError):
            top = self
        if top is not self:
            focus_search_in(top)
            return "break"
        try:
            current = self.nametowidget(self._nb.select())
        except (tk.TclError, KeyError):
            current = None
        if current is not None and focus_search_in(current) is not None:
            return "break"
        self._focus_search()
        return "break"

    def _focus_search(self):
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")

    # ── DB operations ────────────────────────────────────────────────
    def _post_menu(self, menu, widget):
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()

    def _open_file(self, safe=False):
        """Open a database chosen in a file dialog; safe=True: with Safe parse (only the
        built-in parser reads the file, SQLite never opens it)."""
        # 'All files' first: Chrome History, Cookies and many app databases have no extension
        path = filedialog.askopenfilename(
            title="Open SQLite Database (Safe parse)" if safe else "Open SQLite Database",
            filetypes=[("All files", "*.*"),
                       ("SQLite files", "*.db *.sqlite *.sqlite3 *.db3")]
        )
        if not path:
            return
        if not self._confirm_replace("this database"):
            return
        self.set_safe_parse(path, safe)
        self._open_db(path)

    def set_safe_parse(self, path, on):
        """Open `path` with Safe parse from now on (on=True) or normally; the case file keeps
        the choice."""
        norm = os.path.normcase(os.path.abspath(path))
        if on:
            self._safe_paths.add(norm)
        else:
            self._safe_paths.discard(norm)

    def _safe_for(self, path):
        return os.path.normcase(os.path.abspath(path)) in self._safe_paths

    def _confirm_replace(self, what):
        """Ask before a case of several databases is closed for something else (Open, a
        Recent database or case); True to go ahead. A single database is replaced without
        asking, as before."""
        if not self.case.multi:
            return True
        return messagebox.askyesno(
            "Open", "Replace the case of %d databases with %s? The case stays under Recent. "
                    "(Add ▾ adds a database to the case instead.)" % (len(self.case), what),
            parent=self)

    def open_recent(self, path):
        """Recent ▾ › a database: it replaces what is open (asking first for a case)."""
        if self._confirm_replace(os.path.basename(path)):
            self._open_db(path)

    def open_recent_case(self, path):
        """Recent ▾ › a case: it replaces what is open (asking first for a case)."""
        if self._confirm_replace("the saved case"):
            self.reopen_case(path)

    def _open_db(self, path, on_done=None, wait=False):
        """Open one database (closing whatever is open): the case of one database."""
        return self._open_paths([path], on_done=on_done, wait=wait)

    def _open_paths(self, paths, saved=None, active=None, state=None, changes_told=False,
                    on_done=None, wait=False):
        """Open these databases as a new case (closing what is open). saved: {normalised
        path: the case file's record of it} to report files changed since; active: the path
        to make active.

        The databases open on a worker thread, one after the other; each joins the case (and
        the navigator) as soon as it is open, the first one is shown at once, and a progress
        window with Stop shows when opening takes a while. on_done(members) runs on the Tk
        thread when all are open. wait=True (scripts, tests): returns only then, with the
        members opened; otherwise returns [] at once."""
        self._cancel_open()
        # Validate before closing what is open: a bad path (a directory, a Recent entry
        # whose file is gone) must not discard the current case for nothing.
        good = [p for p in paths if os.path.isfile(p)]
        if not good:
            bad = paths[0] if paths else "(no path)"
            messagebox.showerror("Cannot open database",
                                  "Not a database file: %s" % bad, parent=self)
            return []
        paths = good
        if self.case.members:
            self._close_db(confirm=False)
        self.case.clear_protected_folders()
        self._case_path = None
        self._case_state = dict(state or {})
        return self._start_open(paths, saved, active=active, changes_told=changes_told,
                                new_case=True, on_done=on_done, wait=wait)

    def opening(self):
        """True while databases are being opened (on the worker thread), until they are
        shown everywhere."""
        return self._open_state is not None or self._open_finishing is not None

    def wait_opened(self):
        """Block until the databases being opened have joined the case (scripts, tests)."""
        while self._open_state is not None:
            self._open_state["job"].wait()

    def _start_open(self, paths, saved=None, active=None, changes_told=False, new_case=False,
                    on_done=None, wait=False):
        """Open paths into the case on a worker (see _open_paths). A request made while
        another open runs waits for it."""
        if self._open_state is not None:
            self._open_waiting.append((paths, saved, active, changes_told, on_done))
            if wait:
                self.wait_opened()
                want = set(os.path.normcase(os.path.abspath(p)) for p in paths)
                return [m for m in self.case
                        if os.path.normcase(os.path.abspath(m.path)) in want]
            return []
        most = limits.get("case_max_databases")
        seen, todo, failed = set(), [], []
        for path in paths:
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen or self.case.by_path(path) is not None:
                continue                # already open (or listed twice): nothing to do
            seen.add(norm)
            if len(self.case) + len(todo) >= most:
                failed.append((path, "not opened: the case already has %d databases (limit "
                                     "case_max_databases in settings.json)" % most))
                continue
            if not os.path.isfile(path):
                failed.append((path, "no such file (moved or deleted?)"))
                continue
            todo.append(path)
            rec = (saved or {}).get(norm)
            if isinstance(rec, dict) and rec.get("safe_parse") is True:
                self._safe_paths.add(norm)
        safe = dict((p, self._safe_for(p)) for p in todo)
        st = {"paths": todo, "ready": [], "lock": threading.Lock(), "abandoned": False,
              "saved": saved or {}, "active": active, "changes_told": changes_told,
              "new_case": new_case, "members": [], "failed": failed, "on_done": on_done,
              "shown": not new_case, "t0": time.time()}
        self._open_state = st

        def work(job):
            for i, path in enumerate(todo):
                if job.cancelled:
                    break
                job.done = i
                job.status = "Opening %s (%d of %d)…" % (os.path.basename(path), i + 1,
                                                         len(todo))
                t0 = time.time()
                db, err = DB(), None

                def told(text, i=i, path=path):
                    job.status = "Opening %s (%d of %d): %s…" % (os.path.basename(path), i + 1,
                                                                len(todo), text)
                try:
                    db.open(path, safe_parse=safe[path], cancel=lambda: job.cancelled,
                            progress=told)
                    db.tables()
                except Exception as e:      # noqa: BLE001 - reported as not opened
                    e.__traceback__ = None
                    err, db = e, None
                if db is not None and db.session is not None:
                    db.session.release_thread_connection()     # this thread ends soon
                item = (path, db, time.time() - t0, err)
                with st["lock"]:
                    if not st["abandoned"]:
                        st["ready"].append(item)
                        item = None
                if item is not None and item[1] is not None:
                    item[1].close()         # nobody will use it: the open was abandoned
            job.done = len(todo)

        job = Job(self, "Opening %s" % plural(len(todo), "database"), work,
                  lambda result, error, cancelled: self._open_finished(st, error, cancelled),
                  total=max(1, len(todo)), unit="databases", members=[],
                  on_poll=lambda j: self._open_take(st), show_after=0.5)
        st["job"] = job
        if wait:
            st["wait"] = True
            job.wait()
            return list(st["members"])
        return []

    def _open_take(self, st):
        """On the Tk thread: the databases opened since the last look join the case."""
        if st["abandoned"] or self._open_state is not st:
            return
        with st["lock"]:
            items, st["ready"] = st["ready"], []
        added = []
        for path, db, secs, err in items:
            if db is None:
                st["failed"].append((path, err))
                # not open, still evidence: its folder stays out of reach of every write
                self.case.protect_folder(os.path.dirname(os.path.abspath(path)))
                continue
            rec = st["saved"].get(os.path.normcase(os.path.abspath(path)))
            m = self.case.add_db(db, color=(rec or {}).get("color"))
            m.load_time = secs
            m.opened_utc = utc_text()
            for text in limits.problems:    # settings that were refused: shown as Issues
                m.db.session.issues.add("setting_invalid", text, "settings.json")
            if rec is not None:
                m.saved = rec
                m.changes = case_changes(rec)
                if rec.get("color_problem"):    # a colour the case file had wrong: Issues
                    m.db.session.issues.add("case_file_invalid", rec["color_problem"],
                                            "case file")
            tables = m.db.tables()
            m.scope_tables = list(tables)
            m.counts = dict((t, "?") for t in tables)
            self._start_counts(m)
            st["members"].append(m)
            added.append(m)
        if not added:
            return
        for m in added:
            if not st["shown"] and m is self.case.active:
                st["shown"] = True
                self.tags.opened(m.path, m)     # its tags and saved view state
                self._activate_ui()
            else:
                self.tags.add_member(m)
        if not st["job"].finished:
            # the navigator and the case bar show them now; the rest when all are open
            self._populate_schema()
            self._refresh_case_ui()

    def _open_finished(self, st, error, cancelled):
        """On the Tk thread: every database of an open has joined the case (or Stop)."""
        if st["abandoned"] or self._open_state is not st:
            return
        self._open_state = None
        members, failed = st["members"], st["failed"]
        notes = []
        if error is not None:
            notes.append("Opening stopped by an error: %s" % error)
        opened = len(members) + len([1 for p, _e in failed if p in st["paths"]])
        if cancelled and opened < len(st["paths"]):
            notes.append("Stopped: %d of %d databases were not opened." % (
                len(st["paths"]) - opened, len(st["paths"])))
        def rest():
            if self._open_finishing is st:
                self._open_finishing = None
            if st["abandoned"]:
                return
            if st["changes_told"]:
                for m in members:
                    m.changes_reported = True
            if st["new_case"] and not members:
                if failed or notes:
                    messagebox.showerror("Error", "Cannot open database:\n" + "\n".join(
                        ["%s: %s" % (os.path.basename(p), e) for p, e in failed] + notes))
            else:
                self._report_open_problems(failed, notes)
            if st["on_done"] is not None:
                st["on_done"](list(members))
            waiting, self._open_waiting = self._open_waiting, []
            for paths, saved, active, told, done in waiting:
                if self.case.members:
                    self._start_open(paths, saved, active, told, on_done=done)
                else:
                    self._start_open(paths, saved, active, told, new_case=True, on_done=done)
        if members:
            active = st["active"] and self.case.by_path(st["active"])
            if active is not None and active is not self.case.active:
                self.activate_member(active)
            # a step per turn of the event loop, unless a script waits for the open
            self._open_finishing = st
            self._case_changed(spread=not st.get("wait"), then=rest)
        else:
            rest()

    def _cancel_open(self):
        """Abandon an open still running (a new case or Close): the databases it opened
        that have not joined the case are closed on a worker thread."""
        st = self._open_state
        self._open_waiting = []
        if self._open_finishing is not None:
            self._open_finishing["abandoned"] = True      # its last steps do nothing
            self._open_finishing = None
        if st is None:
            return
        self._open_state = None
        with st["lock"]:
            st["abandoned"] = True
            left, st["ready"] = st["ready"], []
        st["job"].cancel()
        dbs = [db for _p, db, _s, _e in left if db is not None]
        if dbs:
            threading.Thread(target=lambda: [db.close() for db in dbs],
                             name="close-abandoned", daemon=True).start()

    def _report_open_problems(self, failed, notes=()):
        """One message for the databases that could not be opened and those that changed
        since the case was saved (the user just asked to open them)."""
        lines = ["%s: cannot be opened (%s)" % (p, e) for p, e in failed] + list(notes)
        for m in self.case:
            if m.changes and not getattr(m, "changes_reported", False):
                m.changes_reported = True
                lines.append("%s changed since the case was saved: %s" % (
                    m.path, "; ".join(m.changes)))
        if lines:
            messagebox.showwarning("Databases", "\n\n".join(lines))

    def _add_databases(self, paths=None, on_done=None, wait=False):
        """Add database(s)…: files from any folders join the open case (or start one)."""
        if paths is None:
            paths = filedialog.askopenfilenames(
                title="Add SQLite Database(s)",
                filetypes=[("All files", "*.*"),
                           ("SQLite files", "*.db *.sqlite *.sqlite3 *.db3")])
            paths = list(paths or ())
        if not paths:
            return []
        if not self.case.members and not self.opening():
            return self._open_paths(paths, on_done=on_done, wait=wait)
        return self._add_paths(paths, on_done=on_done, wait=wait)

    def _open_folder(self, folder=None, wait=False):
        """Open folder…: pick the SQLite databases of a folder; they join the open case (or
        start one)."""
        dlg = OpenFolderDialog(self, folder)
        self._folder_dialog = dlg
        self.wait_window(dlg)
        paths = dlg.result or []
        if not paths:
            return []
        folders = list(getattr(dlg, "evidence_folders", None) or ())

        def done(members):
            # the folder opened and every folder a database was found in stay protected,
            # also those of the databases not chosen (sibling evidence)
            for f in folders:
                self.case.protect_folder(f)
            if self.case.multi and self._overview_added:
                self._nb.select(self._overview)     # the landing page of a case
        return self._add_databases(paths, on_done=done, wait=wait)

    def _add_paths(self, paths, on_done=None, wait=False):
        """More databases join the case (on a worker, see _open_paths): their tags load, the
        schema, case bar and search choices show them, and their links are checked."""
        return self._start_open(paths, on_done=on_done, wait=wait)

    def activate_member(self, member):
        """Make a database of the case the active one: Browse, Forensics, SQL, WAL and
        Freed Pages then show it. Search results, tags and the timeline stay."""
        if member is None or member is self.case.active or member not in self.case.members:
            return
        prev = self.case.active
        # what the one-database tabs showed of the previous database: said where it was
        forensic_lines = self._forensics.results_shown()
        had_sql = bool(self._sql_result_rows)
        had_wal = bool(getattr(self._wal_frame, "_records", None))
        self._deactivate_ui()
        self.case.set_active(member)
        self._navigator.note_used(member)
        self.tags.activate(member)
        self._activate_ui()
        self._refresh_case_ui()
        note ="The results of %s were cleared when %s became the active database; run it "                "again to see this database's." % (prev.name if prev is not None else "?",
                                                  member.name)
        for lbl in forensic_lines:      # each Forensics sub-tab that showed results
            lbl.configure(text=note)
        if had_sql:
            self._sql_status_label.configure(text=note)
        if had_wal and self._wal_tab_added:
            self._wal_frame.rec_status.configure(text=note)

    def remove_member(self, member):
        """Remove from case: close one database (its tags are saved)."""
        if member not in self.case.members:
            return
        if len(self.case) == 1:
            self._close_db(confirm=False)
            return
        was_active = member is self.case.active
        if was_active:
            self._deactivate_ui()
        timeline_stopped = self._timeline.reads(member)
        search_stopped = self._stop_member_workers(member)  # only what reads this database
        self.tags.remove_member(member)
        self.relations.forget_member(member)
        path, old_name = member.path, member.name
        others = [m for m in self.case if m is not member]
        before = dict((m.uid, m.name) for m in others)
        reports = self._verify_before_close([member])
        report = self.case.remove(member, reports.get(member.uid))
        renamed = dict((before[m.uid], m.name) for m in others if before[m.uid] != m.name)
        if report is not None:
            self.activity("close", database=path, unchanged=report.unchanged,
                          checked=report.checked_text(), differences=list(report.differences))
        if report is not None and not report.unchanged:
            messagebox.showwarning("Evidence changed", report.text())
        self._restart_counts()          # the stop above also stopped the other counts
        self.scopes.forget(member.uid)  # no scope covers a database that left
        self._navigator.hits.pop(member.uid, None)
        self._navigator.dates.pop(member.uid, None)
        # its search results go with it (the status says so)
        if any(m is member for m, _t in getattr(self, "_search_work", [])):
            self._search_removed = getattr(self, "_search_removed", []) + [old_name]
        if search_stopped:
            self._search_cancel = True
        self._search_results = [r for r in self._search_results if r.get("dbid") != member.uid]
        self._search_table_hits = dict((k, v) for k, v in self._search_table_hits.items()
                                       if k[0] != member.uid)
        self._search_wal_hits = [r for r in self._search_wal_hits
                                 if r.get("dbid") != member.uid]
        self._search_work = [(m, t) for m, t in getattr(self, "_search_work", [])
                             if m is not member]
        self._search_tables = [k for k in getattr(self, "_search_tables", [])
                               if k[0] != member.uid]
        if was_active:
            self.tags.activate(self.case.active)
            self._activate_ui()
        elif self._browse_source is not None:
            # the linked values were stopped (one may have been read from that database)
            self._browse_lookups.apply(self._browse_table_var.get())
        self._case_changed(timeline=False)
        if timeline_stopped:
            self._timeline.on_open()        # it was reading the database: it starts over
        else:
            self._timeline.forget_member(member, old_name, renamed)
        if self._search_results or self._search_tree.get_children():
            self._finalize_search(len(self._search_tables))

    def _stop_member_workers(self, member, timeout=WORKER_STOP_WAIT):
        """Stop only the work that reads a database leaving the case, and wait for it: jobs
        of that database (or of databases not said), a search or timeline job still running
        over it, the mapping of links (it starts again), the copies and maps that may read
        it, the linked values shown in Browse, and the row counts (they start again). The
        other databases' finished results and running jobs are left alone. (The active
        database's tabs were stopped by _deactivate_ui when it was the active one.) Returns
        True when a running search was stopped."""
        db = member.db
        running = []
        for job in list(getattr(self, "_jobs", ())):
            ms = job.members
            if ms is None or any(x is member or getattr(x, "db", None) is db for x in ms):
                job.cancel()
                running.append(job.thread)
        search_stopped = False
        th = self._search_thread
        if th is not None and th.is_alive() and \
                any(m is member for m, _t in getattr(self, "_search_work", [])):
            self._stop_search()
            search_stopped = True
            running += [th] + list(getattr(self, "_search_threads", ()))
        if self._overview.busy():       # looking for dates, database after database
            self._overview.stop_dates()
            running += self._overview.worker_threads()
        if self._timeline.reads(member):
            self._timeline.stop()
            running += self._timeline.worker_threads()
        running += self.relations.stop_mapping()
        running += self.datamap.forget_member(member)
        lookups = getattr(self, "_browse_lookups", None)
        if lookups is not None:
            lookups.stop()              # a linked table may be in that database
            running += lookups.worker_threads()
        self._count_gen += 1            # counts start again (_restart_counts)
        running += [t for t in [self._bg_count_thread] + list(self._count_threads)
                    if t is not None]
        db.interrupt()                  # every statement running on that database
        me = threading.current_thread()
        deadline = time.time() + timeout
        while True:
            running = [t for t in running if t is not None and t is not me and t.is_alive()]
            if not running or time.time() >= deadline:
                break
            uiyield.beat()              # the workers waited for do not give way
            try:
                for _ in range(100):
                    if not self.tk.dooneevent(WORKER_CALLS_ONLY):
                        break
            except tk.TclError:
                pass
            running[0].join(0.01)
        return search_stopped

    def show_member_evidence(self, member):
        self.activate_member(member)
        self._show_evidence()

    def _deactivate_ui(self):
        """The active database is about to change: stop what reads it and forget what the
        tabs showed of it (the database stays open)."""
        self.tags.remember_view()
        self._sql_query_cancel = True
        browse = self._cancel_browse_workers()
        self._forensics.stop()
        self._wal_frame.stop()
        db = self.db
        running = [t for t in browse + self._forensics.worker_threads()
                   + self._wal_frame.worker_threads()
                   + [self._sql_query_thread] if t is not None and t.is_alive()]
        for t in running:
            db.interrupt(t)
        deadline = time.time() + WORKER_STOP_WAIT
        while running and time.time() < deadline:
            running = [t for t in running if t.is_alive()]
            uiyield.beat()              # the workers waited for do not give way
            try:
                for _ in range(100):
                    if not self.tk.dooneevent(WORKER_CALLS_ONLY):
                        break
            except tk.TclError:
                pass
            if running:
                running[0].join(0.01)
        self._forensics.reset()
        self._browse_dates.reset()
        self._browse_lookups.reset()
        self._browse_source = None
        self._browse_grid.set_source(None)
        self._browse_filter_var.set("")
        self._show_browse_note([])
        self._sql_grid.set_source(None)
        self._sql_result_rows, self._sql_result_cols = [], []
        if hasattr(self, "_sql_status_label"):
            self._sql_status_label.configure(text="")
        self._wal_frame.reset()
        if self._wal_tab_added:
            try:
                self._nb.forget(self._wal_frame)
            except tk.TclError:
                pass
            self._wal_tab_added = False

    def _activate_ui(self):
        """Show the active database in the tabs that work on one database."""
        db, member = self.db, self.case.active
        last_table = self.tags.store.last_table if self.tags.store is not None else None
        self._load_time = member.load_time
        tables = db.tables()

        self._update_db_info()
        self._show_banners()

        # Build browse combo: main tables + views + WAL-only tables
        browse_vals = list(tables) + list(self.db.views())
        if self.db.has_wal:
            wal_only = self.db.wal_tables()
            if wal_only:
                browse_vals.append("---WAL-Only Tables---")
                for wt in wal_only:
                    browse_vals.append(f"WAL: {wt}")
        self._browse_table_combo.configure(values=browse_vals)
        if tables:
            # the table viewed last, else the first one with rows (not an empty one)
            self._browse_table_var.set(last_table if last_table in browse_vals
                                       and last_table != "---WAL-Only Tables---"
                                       else self._first_table_with_rows(tables))
            self._load_browse_table()

        self._forensics.on_open()
        self.relations.refresh_marks()

        # WAL tab: for a database with a -wal file, readable or not (then it says why);
        # always right after Browse
        s = self.db.session
        if self.db.has_wal or (s is not None and s.wal_problem):
            if not self._wal_tab_added:
                self._nb.insert(self._sql_frame, self._wal_frame, text="WAL")
                self._wal_tab_added = True
            self._wal_frame.on_open()
        elif self._wal_tab_added:
            self._nb.forget(self._wal_frame)
            self._wal_tab_added = False
        self._update_sql_notice()
        if hasattr(self, "_sql_status_label"):
            self._sql_status_label.configure(
                text="Ready \u2014 %s is open. Type a query and press Run." % member.name)

        # Search options for WAL rows and deleted records in freed pages: only when a
        # searched database has them
        self._update_search_options()

        self._update_tab_titles()

    def _update_db_info(self):
        """The header: the case (or database) name, its summary and the evidence chips (kept
        current as databases join or leave)."""
        if self.case.active is None or not self.db.ok:
            return
        self._refresh_header()

    def _first_table_with_rows(self, tables, look=50):
        """The first non-internal table whose quick row estimate (max rowid) is not 0; the
        first table when none of the first `look` says so. Internal tables (leading
        underscore, sqlite_*) are skipped so Browse never opens on e.g. _hive_attribution."""
        def _internal(t):
            tl = t.lower()
            return tl.startswith("_") or tl.startswith("sqlite_")
        for t in tables[:look]:
            if _internal(t):
                continue
            n = self.db.approx_count(t)
            if n:
                return t
        for t in tables[:look]:
            if not _internal(t):
                return t
        return tables[0]

    def _start_counts(self, member):
        """Count a database's rows on the row-count worker thread (the session gives it its
        own connection); one worker serves every database of the case in turn."""
        with self._count_lock:
            self._count_queue.append(member)
            th = self._count_worker
            if th is not None and th.is_alive() and self._count_worker_gen == self._count_gen:
                return
            th = threading.Thread(target=self._count_loop, args=(self._count_gen,),
                                  name="row-counts", daemon=True)
            self._count_worker, self._count_worker_gen = th, self._count_gen
        self._count_threads = [t for t in self._count_threads if t.is_alive()] + [th]
        self._bg_count_thread = th
        th.start()

    def _restart_counts(self):
        """Count again the databases whose counts a stop left unfinished."""
        for m in self.case:
            if any(not isinstance(v, int) for v in m.counts.values()):
                self._start_counts(m)

    def _case_changed(self, timeline=True, spread=False, then=None):
        """Databases joined or left the case: show them everywhere and save the case.
        timeline=False: the caller updates the Timeline itself (a database left: the others'
        events stay). spread=True: one step per turn of the event loop (a case of dozens of
        databases made them together one long pause); then() runs after the last step (also
        when a newer change replaces this one before its end)."""
        steps = [self._update_activity_log,
                 self._populate_schema,         # the navigator: its name index and lines
                 self._refresh_case_ui,
                 self._update_search_options,
                 self.relations.start_mapping,  # links checked against the values, in the background
                 self._timeline.on_open if timeline else None,
                 self._update_overview,
                 lambda: self.tags.refresh_views(now=True),
                 self._save_case]
        steps = [s for s in steps if s is not None]
        self._case_change_gen += 1
        gen = self._case_change_gen
        if not spread:
            for step in steps:
                step()
            if then is not None:
                then()
            return

        def run(i):
            if gen != self._case_change_gen or not self.case.members:
                if then is not None:
                    then()              # replaced (the newer change does every step)
                return
            steps[i]()
            if i + 1 < len(steps):
                self.after(1, run, i + 1)
            elif then is not None:
                then()
        run(0)

    def _update_overview(self, select=False):
        """The Overview tab: first, and only for a case of two or more databases."""
        if self.case.multi:
            if not self._overview_added:
                self._nb.insert(0, self._overview, text="Overview")
                self._overview_added = True
            self._overview.on_open()
            if select:
                self._nb.select(self._overview)
        elif self._overview_added:
            self._overview.reset()
            self._nb.forget(self._overview)
            self._overview_added = False

    def _refresh_case_ui(self):
        """The header, the navigator's lines (the active database), the scope pickers, the
        results' Database filter and the breadcrumbs of the one-database tabs."""
        multi = self.case.multi
        self._update_db_info()
        self._close_btn.configure(text="Close case" if multi else "Close")
        self._search_btn_row.show(self._search_db_picker, multi)
        self._search_db_picker.refresh()
        self._sr_filter_bar.show(self._sr_db_lbl, multi)
        self._sr_filter_bar.show(self._sr_db_filter, multi)
        if not multi:
            self._sr_db_filter.set("All")
        self._update_scope_chip()
        self._navigator.apply_filter()
        for crumb in self._crumbs:
            crumb.refresh()
        self._timeline.refresh_scope()

    def _update_tab_titles(self):
        """The tabs keep plain names; the breadcrumb bars say which database they show."""
        for crumb in getattr(self, "_crumbs", ()):
            crumb.refresh()

    # ── The case file ────────────────────────────────────────────────
    def _save_case(self):
        """Keep a case of 2+ databases in the app-data folder (never beside the evidence):
        its databases with size, time and SHA-256, the active one, which ones a search
        covers. It is listed under Recent."""
        if not self.case.multi:
            return None
        # the scopes (global, per feature, saved) and the navigator's pinned databases
        self._case_state["scopes"] = self.scopes.to_state()
        self._case_state["pinned"] = sorted(self._navigator.pinned)
        self._case_state.pop("search_databases", None)
        dbs = []
        for m in self.case:
            fp = m.db.evidence.fingerprints.get("main") if m.db.ok else None
            dbs.append({"path": m.path, "name": m.name, "size": fp.size if fp else None,
                        "mtime_ns": fp.mtime_ns if fp else None,
                        "sha256": (fp.sha256 if fp else None) or (m.saved or {}).get("sha256"),
                        "color": m.color, "safe_parse": self._safe_for(m.path)})
        path = case_file_path([m.path for m in self.case])
        try:
            write_case(path, dbs, active=self.case.active.path if self.case.active else None,
                       state=self._case_state, refuse_in=self.case.evidence_dirs())
        except (OSError, TagError) as e:
            self.tags.messages.append("Case not saved: %s" % e)
            return None
        self._case_path = path
        add_recent_case(self.tags.settings, path, [m.name for m in self.case])
        self.tags.save_settings()
        if any(m.db.ok and not m.sha256() for m in self.case):
            self._schedule_case_hash_check()
        return path

    def _schedule_case_hash_check(self):
        if getattr(self, "_case_hash_after", None) is None:
            self._case_hash_after = self.after(2000, self._case_hash_check)

    def _case_hash_check(self):
        """Once hashed, a database is compared with the SHA-256 its case file recorded; the
        case file then records the hashes."""
        self._case_hash_after = None
        if not self.case.multi:
            return
        waiting, changed = False, []
        for m in self.case:
            sha = m.sha256()
            if not sha:
                waiting = waiting or (m.db.ok and not m.db.evidence.hash_error)
                continue
            diff = sha256_change(m.saved, sha)
            if diff and diff not in m.changes:
                m.changes.append(diff)
                changed.append(m)
        if changed:
            self._refresh_case_ui()
        if waiting:
            self._schedule_case_hash_check()
        else:
            self._save_case()

    def reopen_case(self, path, wait=False, ask=None):
        """Open the databases of a saved case, saying first which ones changed (size, time;
        the SHA-256 is compared once each is hashed) or are missing.

        A network path (engine.evidence.is_network_path: UNC, \\\\?\\UNC\\, a mapped network
        drive) is not checked or opened unless the examiner says so: checking it would make
        Windows connect (and send the user's credentials) to that computer. When the case
        lists one, every path is listed first, the network ones marked 'network path — not
        checked', and the examiner chooses: open them too (Yes), leave them out (No), or
        cancel. ask(title, text) -> True / False / None replaces that question (tests)."""
        try:
            data = read_case(path)
        except TagError as e:
            messagebox.showerror("Case", str(e))
            return []
        dbs = data["databases"]
        network = [d for d in dbs if is_network_path(d["path"])]
        if network:
            ask = ask or (lambda title, text: messagebox.askyesnocancel(title, text,
                                                                        parent=self))
            lines = ["%s%s" % (d["path"], "   (network path — not checked)"
                               if d in network else "") for d in dbs]
            answer = ask("Case", "This case lists %d database%s:\n\n%s\n\n%d of them %s on "
                                 "another computer. Checking or opening a network path makes "
                                 "Windows connect to that computer with your sign-in.\n\n"
                                 "Yes: open the network paths too.\nNo: leave them out.\n"
                                 "Cancel: do not open the case." % (
                                     len(dbs), "" if len(dbs) == 1 else "s", "\n".join(lines),
                                     len(network), "is" if len(network) == 1 else "are"))
            if answer is None:
                return []
            if not answer:
                dbs = [d for d in dbs if d not in network]
                if not dbs:
                    return []
        saved = dict((os.path.normcase(os.path.abspath(d["path"])), d) for d in dbs)
        problems = []
        for d in dbs:
            diff = case_changes(d)
            if diff:
                problems.append("%s: %s" % (d["path"], "; ".join(diff)))
        if problems and not messagebox.askyesno(
                "Case", "Some databases of this case are not as they were when the case was "
                        "saved:\n\n%s\n\nOpen the case anyway? Missing files are left out."
                        % "\n".join(problems)):
            return []
        paths = [d["path"] for d in dbs if os.path.isfile(d["path"])]
        # the changes were just shown: the chips keep them ('changed'), no second message
        state = data.get("state") or {}

        def done(members):
            if not members:
                return
            pinned = state.get("pinned")
            if isinstance(pinned, list):
                self._navigator.pinned = set(p for p in pinned if isinstance(p, str))
            scopes = state.get("scopes")
            sel = state.get("search_databases")         # a case saved by an earlier version
            if not isinstance(scopes, dict) and isinstance(sel, list):
                scopes = {"global": sel}
            if isinstance(scopes, dict):
                self.scopes.load_state(scopes)
            self._navigator.apply_filter()
            self._update_search_options()
        return self._open_paths(paths, saved=saved, active=data.get("active"), state=state,
                                changes_told=bool(problems), on_done=done, wait=wait)

    def _save_navigator_state(self):
        self._save_case()

    def _count_loop(self, gen):
        """The row-count worker: the databases queued (one after the other, not one thread
        each: a case of many large databases must not start as many full counts at once).
        First every queued database's instant approximations, then the exact counts."""
        try:
            while gen == self._count_gen:
                with self._count_lock:
                    batch = list(self._count_queue)
                    del self._count_queue[:]
                    if not batch:
                        self._count_worker = None
                        return
                for m in batch:
                    if m.db.ok:
                        self._bg_count(list(m.db.tables()), gen, m.counts, m.db, exact=False)
                for m in batch:
                    if m.db.ok:
                        self._bg_count(list(m.db.tables()), gen, m.counts, m.db, approx=False)
            with self._count_lock:
                if self._count_worker is threading.current_thread():
                    self._count_worker = None
        finally:
            self._release_worker_connection()

    def _bg_count(self, tables, gen, cache, db=None, approx=True, exact=True):
        """Row counts on a worker thread, written into `cache` (that database's count dict).

        Pass 1: max(rowid) approximations for SQL-served rowid tables (instant).
        Pass 2: exact COUNT(*) or native B-tree counts, replacing the approximations.

        `gen` identifies the open this thread belongs to. Closing (or a stop) bumps
        self._count_gen, so a thread still finishing a count stops; a database closed on its
        own (removed from the case) stops its thread through its session.
        """
        db = db if db is not None else self.db
        session = db.session

        def stale():
            return gen != self._count_gen or db.session is not session

        for t in (tables if approx else ()):
            if stale():
                return
            approx_n = db.approx_count(t)
            if approx_n is not None and not isinstance(cache.get(t), int):
                cache[t] = f"~{approx_n}"
        if not stale():
            self._after_safe(0, self._update_schema_counts)
        if not exact:
            return
        last_update = 0
        for t in tables:
            if stale():
                return
            try:
                cache[t] = db.count(t)
            except Exception:
                if not isinstance(cache.get(t), int):
                    cache[t] = "?"
            now = time.time()
            if now - last_update >= 0.3:
                last_update = now
                self._after_safe(0, self._update_schema_counts)
        if not stale():
            self._after_safe(0, self._update_schema_counts)

    def _update_schema_counts(self):
        """Row counts changed (the count worker): the navigator's and the Overview's lines."""
        self._navigator.refresh_counts()
        if self._overview_added:
            if getattr(self, "_overview_counts_after", None) is None:
                self._overview_counts_after = self.after(500, self._overview_counts)

    def _overview_counts(self):
        self._overview_counts_after = None
        if self._overview_added:
            self._overview.refresh_counts()

    def _on_app_close(self):
        """Close the database (verifying the evidence) and exit."""
        self._close_db(confirm=False, wait=True)
        self.wait_closed()              # a verification still running from an earlier close
        self.destroy()

    def closing(self):
        """True while the evidence of databases just closed is being verified."""
        return self._close_verify is not None

    def wait_closed(self):
        """Block until the evidence of the databases closed last is verified (exit, tests)."""
        st = self._close_verify
        if st is not None:
            st["thread"].join()
            self._close_verified(st)

    def _verify_after_close(self, pending, log):
        """Verify the evidence of databases just closed [(name, path, EvidenceSet)] on a
        worker thread: size and time, and the SHA-256 again up to the limit
        verify_rehash_bytes (a small window with 'Skip SHA-256' shows when that takes more
        than half a second). The results go to the case's activity log, the header and, for a
        changed file, a warning."""
        limit = limits.get("verify_rehash_bytes")
        total = sum(ev.total_bytes for _n, _p, ev in pending if ev.rehash_on_close(limit))
        st = {"skip": False, "done": 0, "name": "", "reports": [], "end": False,
              "t0": time.time(), "win": None, "total": total, "shown": False}

        def work():
            try:
                for name, path, ev in pending:
                    st["name"] = name

                    def progress(n):
                        st["done"] += n
                    try:
                        rep = ev.verify_on_close(limit, lambda: st["skip"], progress)
                    except Exception:   # noqa: BLE001 - the hash failed: size and time
                        rep = ev.verify(rehash=False)
                    st["reports"].append((name, path, rep))
                    if log is not None:
                        try:
                            log.log("close", database=path, unchanged=rep.unchanged,
                                    checked=rep.checked_text(),
                                    differences=list(rep.differences))
                        except Exception:       # noqa: BLE001 - the log never stops it
                            pass
            except Exception as e:      # noqa: BLE001 - said in the header
                e.__traceback__ = None
                st["error"] = e
            finally:
                st["end"] = True
        th = st["thread"] = threading.Thread(target=work, name="verify-after-close",
                                             daemon=True)
        prev = self._close_verify
        self._close_verify = st
        if prev is not None:
            self._close_verified(prev)  # an earlier close still verifying: its results too
        th.start()
        self.after(50, self._poll_close_verify, st)

    def _poll_close_verify(self, st):
        if st.get("reported"):
            return
        if not st["end"]:
            win = st["win"]
            if win is None and st["total"] and time.time() - st["t0"] > 0.5:
                win = st["win"] = tk.Toplevel(self)
                win.title("Verifying the evidence of the closed databases")
                win.configure(bg=C["bg"])
                win.transient(self)
                win.resizable(False, False)
                win.protocol("WM_DELETE_WINDOW", lambda: st.update(skip=True))
                st["lbl"] = tk.Label(win, text="", bg=C["bg"], anchor="w", justify="left",
                                     width=60, wraplength=420)
                st["lbl"].pack(fill="x", padx=12, pady=(10, 4))
                st["bar"] = ttk.Progressbar(win, mode="determinate",
                                            maximum=max(st["total"], 1), length=420)
                st["bar"].pack(fill="x", padx=12, pady=4)
                ttk.Button(win, text="Skip SHA-256 (size and time only)",
                           command=lambda: st.update(skip=True)).pack(pady=(4, 10))
                place_over(win, self)
            if win is not None:
                try:
                    st["lbl"].configure(text="Re-computing the SHA-256 of %s: %s of %s%s" % (
                        st["name"], fmtb(st["done"]), fmtb(st["total"]),
                        " — skipping, checking size and time…" if st["skip"] else ""))
                    st["bar"].configure(value=min(st["done"], st["total"]))
                except tk.TclError:
                    st["skip"] = True
            self.after(100, self._poll_close_verify, st)
            return
        self._close_verified(st)

    def _close_verified(self, st):
        """The evidence of the closed databases is verified: say what was checked, and warn
        about a changed file."""
        if st.get("reported"):
            return
        st["reported"] = True
        if self._close_verify is st:
            self._close_verify = None
        if st["win"] is not None:
            try:
                st["win"].destroy()
            except tk.TclError:
                pass
        changed, closed = [], []
        for name, _path, rep in st["reports"]:
            if not rep.unchanged:
                changed.append(rep.text())
            closed.append("%s %s (%s)" % (name, "verified unchanged" if rep.unchanged
                                          else "CHANGED", rep.checked_text()))
        if st.get("error") is not None:
            closed.append("verification stopped: %s" % st["error"])
        if not self.case.members and not self.opening():
            self._db_info.set_text("No database loaded" + (
                "  ·  last closed: " + "; ".join(closed) if closed else ""))
        self.last_closed = closed
        if changed:
            messagebox.showwarning("Evidence changed", "\n\n".join(changed))

    def destroy(self):
        """Every timer still pending is cancelled first: it would otherwise call a command the
        destroy deletes."""
        uiyield.stop()                  # the workers no longer wait for this thread
        cancel_all_afters(self)
        tk.Tk.destroy(self)

    # ── The activity log (engine.activity) ───────────────────────────
    def activity(self, kind, **fields):
        """Note what the examiner did (opened, searched, exported, verified...) in the case's
        activity log in the app-data folder; never raises."""
        log = getattr(self, "_activity_log", None)
        if log is None:
            return False
        try:
            return log.log(kind, **fields)
        except Exception:               # noqa: BLE001 - the log never stops the work
            return False

    def activity_many(self, items):
        """Several activity entries ((kind, fields) pairs) in one write; never raises."""
        log = getattr(self, "_activity_log", None)
        if log is None:
            return False
        try:
            return log.log_many(items)
        except Exception:               # noqa: BLE001 - the log never stops the work
            return False

    # ── Tags (for every tab) ─────────────────────────────────────────
    def tag_entries(self, entries, tag):
        """Tag rows: entries are engine.tags.TagEntry objects (entry_from_db_row,
        entry_from_wal_record, entry_from_freelist, entry_from_record...). A tag of that name
        is made when unknown. Returns the number of rows newly tagged (0 with no database)."""
        return self.tags.tag_entries(entries, tag)

    def tag_menu(self, parent_menu, get_entries, label="Tag"):
        """Add the 'Tag' submenu (a check item per tag, New tag..., Edit note..., Remove all
        tags) to a right-click menu, for the rows get_entries() returns (called at once).
        Returns the submenu."""
        return self.tags.tag_menu(parent_menu, get_entries, label)

    def _stop_workers(self, timeout=WORKER_STOP_WAIT):
        """Cancel the count, search and SQL-tab worker threads and wait for them to end.

        Their statements are interrupted, then this waits up to `timeout` seconds, so the DB is
        not closed under a running worker (the session never closes a connection another
        thread still holds either). It does not block in join(): a worker may itself be waiting
        for this Tk thread to serve an after() call or a variable read, so those calls are
        served meanwhile. User input and timers are not processed during the wait, so no other
        open or close can start inside this one. Returns the threads still running.
        """
        self._count_gen += 1            # the count thread of this open stops writing counts
        jobs = list(getattr(self, "_jobs", ()))
        for job in jobs:
            job.cancel()                # exports, verification (they end at the next row)
        wal = getattr(self, "_wal_frame", None)
        if wal is not None:
            wal.stop()                  # the WAL records being compared
        self._search_cancel = True
        self._sql_query_cancel = True
        browse = self._cancel_browse_workers()
        self._forensics.stop()          # carving, history, recovery, audit, reports
        self.relations.stop()           # related rows, relation maps and value checks
        self.datamap.stop()             # Copy with related, the Database Map
        overview = getattr(self, "_overview", None)
        if overview is not None and overview.busy():  # looking for dates, database after database
            overview.stop_dates()
        self._timeline.stop()
        lookups = getattr(self, "_browse_lookups", None)
        if lookups is not None:
            lookups.stop()              # a linked table being read
        dates = getattr(self, "_browse_dates", None)     # a sample for 'Show as date'
        self.case.interrupt()           # stop the statements they are running now (every db)
        me = threading.current_thread()
        running = [t for t in [self._bg_count_thread, self._search_thread,
                               self._sql_query_thread] + list(self._count_threads)
                   if t is not None and t is not me]
        running += [t for t in browse + self._forensics.worker_threads() + self.tags.worker_threads()
                    + self.relations.worker_threads() + self._timeline.worker_threads()
                    + (lookups.worker_threads() if lookups is not None else [])
                    + (dates.worker_threads() if dates is not None else [])
                    + self.datamap.worker_threads()
                   + (overview.worker_threads() if overview is not None else [])
                    + [j.thread for j in jobs if j.thread.is_alive()]
                    + (wal.worker_threads() if wal is not None else [])
                    if t is not me]
        deadline = time.time() + timeout
        while True:
            running = [t for t in running if t.is_alive()]
            if not running or time.time() >= deadline:
                return running
            uiyield.beat()              # the workers waited for do not give way
            try:
                for _ in range(100):
                    if not self.tk.dooneevent(WORKER_CALLS_ONLY):
                        break
            except tk.TclError:
                pass                    # Tk is gone: a worker's call into it fails and it ends
            running[0].join(0.01)

    def _cancel_browse_workers(self):
        """Drop the Browse grid's queued window reads and row counts; returns the threads
        still running one (they end once their interrupted statement returns)."""
        self._browse_count_gen += 1
        self._browse_pos_gen += 1
        self._browse_pos_busy = False
        self._browse_grid.cancel_fetches()
        self._browse_counter.cancel()
        return self._browse_grid.worker_threads() + self._browse_counter.threads()

    def _verify_before_close(self, members):
        """{uid: VerifyReport} of the databases about to close: size and time, and the SHA-256
        again up to the limit verify_rehash_bytes. The re-hash runs on a worker thread; while
        it runs a small window shows which file and how far, with 'Skip SHA-256 (size and time
        only)'. Only that window takes input meanwhile (the close waits for it)."""
        limit = limits.get("verify_rehash_bytes")
        items = [(m, m.db.evidence) for m in members if m.db.ok]
        reports = {}
        if not any(ev.rehash_on_close(limit) for _m, ev in items):
            for m, ev in items:         # size and time only: quick, no window
                reports[m.uid] = ev.verify_on_close(limit)
            return reports
        total = sum(ev.total_bytes for _m, ev in items if ev.rehash_on_close(limit))
        state = {"skip": False, "done": 0, "name": "", "error": None, "end": False}

        def work():
            try:
                for m, ev in items:
                    state["name"] = m.name

                    def progress(n):
                        state["done"] += n
                    reports[m.uid] = ev.verify_on_close(limit, lambda: state["skip"], progress)
            except Exception as e:      # noqa: BLE001 - reported; the close goes on
                state["error"] = e
            finally:
                state["end"] = True
        th = threading.Thread(target=work, name="verify-on-close", daemon=True)
        th.start()
        th.join(0.3)                    # a small case is done before any window shows
        if th.is_alive():
            win = tk.Toplevel(self)
            win.title("Verifying the evidence before closing")
            win.configure(bg=C["bg"])
            win.transient(self)
            win.resizable(False, False)
            win.protocol("WM_DELETE_WINDOW", lambda: state.update(skip=True))
            lbl = tk.Label(win, text="", bg=C["bg"], anchor="w", justify="left", width=60,
                           wraplength=420)
            lbl.pack(fill="x", padx=12, pady=(10, 4))
            bar = ttk.Progressbar(win, mode="determinate", maximum=max(total, 1), length=420)
            bar.pack(fill="x", padx=12, pady=4)
            skip_btn = ttk.Button(win, text="Skip SHA-256 (size and time only)",
                                  command=lambda: state.update(skip=True))
            skip_btn.pack(pady=(4, 10))
            place_over(win, self)
            self._verify_close_win = win
            # closing the main window meanwhile only skips the re-hash (the close goes on)
            self.protocol("WM_DELETE_WINDOW", lambda: state.update(skip=True))
            try:
                win.grab_set()
                win.focus_set()
            except tk.TclError:
                pass
            while th.is_alive():
                try:
                    if not win.winfo_exists():
                        state["skip"] = True
                    else:
                        lbl.configure(text="Re-computing the SHA-256 of %s: %s of %s%s" % (
                            state["name"], fmtb(state["done"]), fmtb(total),
                            " — skipping, checking size and time…" if state["skip"] else ""))
                        bar.configure(value=min(state["done"], total))
                    self.update()
                except tk.TclError:
                    state["skip"] = True
                th.join(0.05)
            self._verify_close_win = None
            try:
                self.protocol("WM_DELETE_WINDOW", self._on_app_close)
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass
        th.join()
        if state["error"] is not None:
            for m, ev in items:         # what could not be verified with the hash: size/time
                if m.uid not in reports:
                    reports[m.uid] = ev.verify(rehash=False)
        return reports

    def _close_db(self, confirm=True, wait=False):
        """Close every open database (the whole case), verifying each one's evidence (size and
        time, and SHA-256 again up to the limit verify_rehash_bytes; the header then says what
        was verified). confirm: ask first when a case of several databases would close.

        The verification runs on a worker thread once the databases are closed (it needs only
        their files): the header says it is verifying, then what was verified; a changed file
        is reported as before. wait=True (exit, scripts): verify first, then close."""
        if confirm and self.case.multi and not messagebox.askyesno(
                "Close case", "Close the case of %d databases? It stays under Recent."
                % len(self.case), parent=self):
            return False
        self._cancel_open()             # databases still opening never join
        if self.case.multi:
            self._save_case()           # the case as it is now (the search choice)
        self.tags.closing()             # saves the tags and the Browse view state
        self._stop_workers()            # nothing may be closed under a running worker
        members = list(self.case)
        reports = self._verify_before_close(members) if wait else None
        pending = [(m.name, m.path, m.db.evidence) for m in members if m.db.ok]
        closed_log = self._activity_log
        changed, closed = [], []
        for m in members:
            name, path = m.name, m.path
            if reports is None:
                self.case.remove(m, False)      # verified below, on a worker thread
                continue
            # leaves a connection still in use to its thread
            report = self.case.remove(m, reports.get(m.uid))
            if report is None:
                continue
            self.activity("close", database=path, unchanged=report.unchanged,
                          checked=report.checked_text(), differences=list(report.differences))
            if not report.unchanged:
                changed.append(report.text())
            closed.append("%s %s (%s)" % (name, "verified unchanged" if report.unchanged
                                          else "CHANGED", report.checked_text()))
        if changed:
            messagebox.showwarning("Evidence changed", "\n\n".join(changed))
        self._activity_log = None
        self._case_path, self._case_state = None, {}
        self._count_threads = []
        with self._count_lock:
            del self._count_queue[:]
        self.relations.close_all()      # their rows belong to the databases just closed
        self.datamap.close_all()
        self._no_counts, self._no_scope = {}, []
        self._search_results = []
        self._search_errors = []
        self._sr_reset_groups()
        self._sr_filtered = []
        self._forensics.reset()
        self._timeline.reset()
        self._browse_dates.reset()
        self._browse_lookups.reset()
        self._browse_source = None
        self._browse_grid.set_source(None)
        self._browse_filter_var.set("")
        self._show_browse_note([])
        self._update_browse_status()
        self._banners = []
        self._overview.reset()
        self._navigator.hits, self._navigator.dates = {}, {}
        self._navigator.pinned = set()
        self.scopes.global_sel, self.scopes.own, self.scopes.saved = None, {}, {}
        self._chips_issue_count = None
        if self._issues_win is not None and self._issues_win.winfo_exists():
            self._issues_win.destroy()
        self._refresh_issue_btn()
        # Clear SQL editor state
        self._sql_grid.set_source(None)
        if hasattr(self, '_sql_status_label'):
            self._sql_status_label.configure(text='Open a database to start querying.')
        if hasattr(self, '_sql_col_info'):
            self._sql_col_info.configure(text='')
        self._sql_result_rows = []
        self._sql_result_cols = []
        self._search_tree.delete(*self._search_tree.get_children())
        self._browse_table_combo.configure(values=[])
        self._browse_table_var.set("")
        self._refresh_header()
        if reports is None and pending:
            self._db_info.set_text("No database loaded  ·  verifying the evidence of %s "
                                   "just closed…" % plural(len(pending), "database"))
            self._verify_after_close(pending, closed_log)
        else:
            self.last_closed = closed
            self._db_info.set_text("No database loaded" + (
                "  ·  last closed: " + "; ".join(closed) if closed else ""))
        # Remove WAL tab if present
        self._wal_frame.reset()
        if self._wal_tab_added:
            try:
                self._nb.forget(self._wal_frame)
            except tk.TclError:
                pass
            self._wal_tab_added = False
        self._update_sql_notice()
        self._search_work, self._search_tables = [], []
        self._navigator.rebuild()
        self._update_overview()
        self._refresh_case_ui()
        self._search_status.set("Ready")

    def _show_info(self):
        if not self.db.ok:
            messagebox.showinfo("Info", "No database loaded")
            return
        m = self.db.meta()
        total_rows = sum(_int_count(v) for v in self._count_cache.values())
        info = (
            f"Path: {m.get('path', '')}\n"
            f"Size: {fmtb(m.get('size', 0))}\n"
            f"Open mode: {mode_label(m.get('mode', ''), long=True)}\n"
            f"Page size: {m.get('page_size', '')}\n"
            f"Page count: {m.get('page_count', '')}\n"
            f"Journal mode: {m.get('journal_mode', '')}\n"
            f"Encoding: {m.get('encoding', '')}\n"
            f"Reserved bytes/page: {m.get('reserved_bytes', '')}\n"
            f"Auto vacuum: {m.get('auto_vacuum', '')}\n"
            f"User version: {m.get('user_version', '')}\n"
            f"Freelist count: {m.get('freelist_count', '')}\n"
            f"Total rows: {total_rows:,}\n"
            f"Tables: {len(self.db.tables())}\n"
            f"Views: {len(self.db.views())}\n"
            f"Indexes: {len(self.db.all_indexes())}\n"
            f"Triggers: {len(self.db.triggers())}"
        )
        if self.db.has_wal:
            ws = self.db.wal.summary()
            info += f"\n\nWAL File:\nWAL size: {fmtb(ws.get('wal_size', 0))}\n"
            info += f"WAL frames: {ws.get('total_frames', 0)} ({ws.get('commits', 0)} commits)\n"
            for state, (label, _fg, _bg, _desc) in WAL_STATES.items():
                info += f"  {label}: {ws.get(state, 0)}\n"
            info += f"Unique pages: {ws.get('unique_pages', 0)}"
        messagebox.showinfo("Database info" + (" - " + self.case.active.name if self.case.multi
                                               else ""), info, parent=self)

    def _show_banners(self):
        """Engine status (mode, WAL, journal, collations) of the active database: kept for the
        status window; the header shows it as one evidence chip and a warnings chip."""
        rank = {"error": 0, "warning": 1, "info": 2}
        self._chips_issue_count = self._issue_count()
        self._banners = sorted(self.db.banners(), key=lambda b: rank.get(b.level, 3))
        self._refresh_header()
        self._refresh_issue_btn()

    def status_chips(self):
        """(evidence chip text, warnings chip text or '') of the header (tests)."""
        warn = self._warn_chip.text if self._warn_chip.winfo_manager() else ""
        ev = self._evidence_chip.text if self._evidence_chip.winfo_manager() else ""
        return ev, warn

    def status_detail_text(self):
        """The status of every open database: those with warnings or errors first, each
        with its notes; the rest in one line per open mode."""
        rank = {"error": 0, "warning": 1, "info": 2}
        warned, quiet = [], OrderedDict()
        for m in self.case:
            if not m.db.ok:
                continue
            bs = sorted(m.db.banners(), key=lambda b: rank.get(b.level, 3))
            if any(b.level in ("warning", "error") for b in bs):
                warned.append((m, bs))
            else:
                quiet.setdefault(mode_label(m.db.mode, long=True), []).append((m, bs))
        lines = []
        for m, bs in warned:
            lines.append("%s  (%s)" % (m.name, m.path))
            for b in bs:
                lines.append("  %s %s: %s" % ("⚠" if b.level != "info" else "·", b.short, b.text))
            lines.append("")
        for mode, ms in quiet.items():
            if len(ms) == 1 and not warned and len(quiet) == 1:
                m, bs = ms[0]
                lines.append("%s  (%s)" % (m.name, m.path))
                for b in bs:
                    lines.append("  · %s: %s" % (b.short, b.text))
            else:
                lines.append("%s: %s, no warnings (%s)" % (
                    plural(len(ms), "database"), mode, ", ".join(m.name for m, _b in ms)))
        return "\n".join(lines).strip()

    def _show_status_detail(self):
        """The status of every open database, in a window (never a cut message box)."""
        if not any(m.db.ok for m in self.case):
            return None
        warned = sum(1 for m in self.case if member_warnings(m))
        intro = "Every database is opened read-only; nothing is written next to the " \
                "evidence." + (" %s with warnings, listed first." % plural(warned, "database")
                               if warned else "")
        return TextWindow(self, "Evidence status", intro, self.status_detail_text())

    def _issue_logs(self):
        """[(label, IssueLog)] of the active database: what reading it met, then what the
        forensic scans met (label 'Forensics'), once they have run."""
        if not self.db.ok:
            return []
        s = self.db.session
        logs = [("", s.issues)]
        fx = s.__dict__.get("_forensics")          # created by the first forensic scan
        if fx is not None:
            logs += [("Forensics", log) for log in fx.issue_logs()]
        return logs

    def _issue_count(self):
        """Distinct issues of the active database (the Issues window lists each once)."""
        logs = self._issue_logs()
        if len(logs) == 1:
            return logs[0][1].distinct_count()
        seen = set()
        for label, log in logs:
            seen.update((label,) + key for key in list(log._distinct))
        return len(seen)

    def _refresh_issue_btn(self):
        """'Issues (N)' in the header only while there are issues (the Database menu always
        lists them)."""
        n = self._issue_count()
        text = "Issues (%d)" % n if n else "Issues"
        if self._issues_btn.cget("text") != text:
            self._issues_btn.configure(text=text)
        if n and not self._issues_btn.winfo_manager():
            self._issues_btn.pack(side="right", padx=3, before=self._db_btn)
        elif not n and self._issues_btn.winfo_manager():
            self._issues_btn.pack_forget()

    def _refresh_status(self):
        """Keep the 'Issues (N)' button and the chips current: workers (counts, searches) add
        issues too, and a table SQLite fails on mid-session gets its own chip."""
        if self.db.ok and self._issue_count() != getattr(self, "_chips_issue_count", None):
            m = self.case.active
            if m is not None:
                m._warn_cache = None        # a new issue may be a new banner: ask again
            self._show_banners()
        else:
            self._refresh_issue_btn()

    def _poll_issue_count(self):
        self._refresh_status()
        self._log_new_hashes()
        self.after(1000, self._poll_issue_count)

    def _show_issues(self):
        """List every Issue the engine recorded: what it skipped, substituted or guessed."""
        if not self.db.ok:
            messagebox.showinfo("Issues", "No database loaded")
            return
        if self._issues_win is not None and self._issues_win.winfo_exists():
            self._issues_win.destroy()
        logs = self._issue_logs()
        items = [(label, it) for label, log in logs for it in log.items]
        total = self._issue_count()
        dropped = sum(log.dropped for _l, log in logs)
        win = self._issues_win = tk.Toplevel(self)
        win.title("Issues (%d)" % total)
        fit_geometry(win, 980, 420)
        win.configure(bg=C["bg"])
        head = ("%s recorded while reading %s (Times: how often each was met; 'Forensics: ' "
                "marks what the Forensics scans met). Nothing is hidden: rows are shown from a "
                "fallback where possible, and each entry says what happened and where."
                % (plural(total, "issue"), self.member_label(self.case.active, None)
                   or "this database"))
        if dropped:
            head += ("  (%s more were not kept: a log keeps %s, limit issues_kept.)"
                     % (format(dropped, ","), format(limits.get("issues_kept"), ",")))
        tk.Label(win, text=head, bg=C["bg"], fg=C["text"], anchor="w", justify="left",
                 wraplength=940, font=F["body"]).pack(fill="x", padx=8, pady=(8, 4))
        bar = tk.Frame(win, bg=C["bg"])
        bar.pack(fill="x", padx=8, pady=8, side="bottom")
        search = SearchBox(win, placeholder="Find an issue…", delay=0, find_button=False,
                           primary=True, width=30)
        search.pack(fill="x", padx=8, pady=(0, 4))
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8)
        cols = ("Severity", "Kind", "Where", "Detail", "Times")
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, w in zip(cols, (70, 170, 220, 470, 50)):
            tree.heading(c, text=c)
            tree.column(c, width=w, stretch=(c == "Detail"))
        ysb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        grouped = {}        # the same row read again (browse, search, detail) logs the same issue
        def where_of(label, it):
            return ("%s: %s" % (label, it.where)) if label else it.where
        for label, it in items:
            key = (it.severity, it.kind, where_of(label, it), it.detail)
            grouped[key] = grouped.get(key, 0) + 1
        for (severity, kind, where, detail), times in grouped.items():
            tree.insert("", "end", values=(severity, kind, where, detail, times),
                        tags=(severity,))
        tree.tag_configure("error", foreground=C["red"])
        tree.tag_configure("warning", foreground=C["orange"])
        TreeviewTooltip(tree)
        self._issues_tree = tree
        self._issues_filter = TreeFilter(tree, search, "issue", "issues")

        def copy():
            win.clipboard_clear()
            win.clipboard_append("\n".join("%s\t%s\t%s\t%s" % (
                it.severity, it.kind, where_of(label, it), it.detail) for label, it in items))
        ttk.Button(bar, text="Copy as text", command=copy).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self._show_issues).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

    def _evidence_text(self, member):
        """What the Evidence window says about one database: how it was opened, every file
        with size, modification time (UTC) and SHA-256, the files next to it the tool does not
        use, and the tool and versions."""
        db = member.db
        ev = db.evidence
        lines = ["%s  —  opened read-only (%s) at %s" % (
                     member.path, mode_label(db.mode, long=True),
                     getattr(member, "opened_utc", "?")),
                 "Nothing is written to: %s" % ev.directory, ""]
        for fp in ev.summary():
            sha = fp["sha256"]
            if not sha:
                sha = ("not hashed: %s" % ev.hash_error) if ev.hash_error else \
                    "hashing… %d%%" % (100 * ev.hashed_bytes // max(ev.total_bytes, 1))
            lines += ["%-8s %s" % (fp["role"], fp["path"]),
                      "         size %s bytes   modified %s (mtime_ns %s)" % (
                          format(fp["size"], ","), mtime_text(fp["mtime_ns"]), fp["mtime_ns"]),
                      "         SHA-256 %s" % sha]
            if fp.get("sha256_note"):
                lines.append("         (%s)" % fp["sha256_note"])
            lines.append("")
        others = ev.other_files()
        if others:
            lines.append("Other files next to the database (NOT used by the tool, listed so "
                         "nothing is missed):")
            for o in others:
                lines.append("  %s  (%s, %s bytes, modified %s)" % (
                    o["name"], o["kind"], format(o["size"], ","), mtime_text(o["mtime_ns"])))
            more = getattr(ev, "other_files_more", 0)
            if more:
                lines.append("  … and %d more" % more)
            lines.append("")
        lines.append("SQLite GUI Analyzer %s, Python %s, SQLite %s" % (
            VERSION, sys.version.split()[0], sqlite3.sqlite_version))
        return "\n".join(lines)

    def _show_evidence(self):
        """Evidence window: the original files with size, time and SHA-256, the files next to
        them the tool does not use, verification on a worker thread (Verify now), and a check
        against an expected hash (e.g. from the acquisition)."""
        member = self.case.active
        if member is None or not self.db.ok:
            messagebox.showinfo("Evidence", "No database loaded", parent=self)
            return
        ev = self.db.evidence
        win = tk.Toplevel(self)
        win.title("Evidence and verification" + (" - " + member.name if self.case.multi
                                                 else ""))
        fit_geometry(win, 820, 460)
        win.configure(bg=C["bg"])
        txt = tk.Text(win, font=F["mono"], wrap="word", bg=C["bg2"], relief="flat")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        bar = FlowFrame(win)
        bar.pack(side="bottom", fill="x", padx=8, pady=8)
        search = SearchBox(win, placeholder="Find a file, hash or word…", primary=True,
                           width=30)
        search.pack(fill="x", padx=8, pady=(8, 0))
        sb.pack(side="right", fill="y", pady=8)
        txt.pack(fill="both", expand=True, padx=(8, 0), pady=8)
        win.finder = TextFind(txt, search)
        self._evidence_win = win

        def render():
            if not win.winfo_exists():
                return
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("end", self._evidence_text(member))
            txt.configure(state="disabled")
            if not ev.hashing_done and not ev.hash_error:
                win.after(500, render)

        def verify():
            rehash = ev.hashing_done

            def work(job):
                job.status = "Verifying %s: size, time%s…" % (
                    member.name, " and SHA-256" if rehash else "")

                def progress(n):
                    job.done += n
                return ev.verify(rehash=rehash, cancel=lambda: job.cancelled,
                                 progress=progress)

            def done(report, error, cancelled):
                if error is not None:
                    messagebox.showerror("Verification", str(error), parent=win)
                    return
                self.activity("verify", database=member.path, unchanged=report.unchanged,
                              checked=report.checked_text(),
                              differences=list(report.differences), stopped=cancelled)
                text = report.text()
                if cancelled:
                    text = ("STOPPED: the SHA-256 was not re-computed for every file.\n\n"
                            + text)
                (messagebox.showinfo if report.unchanged and not cancelled
                 else messagebox.showwarning)("Verification", text, parent=win)
            Job(self, "Verifying the evidence", work, done, members=[member],
                total=ev.total_bytes if rehash else None, unit="bytes")

        def expected():
            self._compare_expected_hash(member, win)

        def copy():
            win.clipboard_clear()
            win.clipboard_append(json.dumps(ev.summary(), indent=2))
        bar.add(ttk.Button(bar, text="Verify now", command=verify))
        bar.add(ttk.Button(bar, text="Compare with expected hash…", command=expected))
        bar.add(ttk.Button(bar, text="Copy as JSON", command=copy))
        bar.add(ttk.Button(bar, text="Close", command=win.destroy), gap=24)
        render()
        return win

    def _compare_expected_hash(self, member, parent):
        """Paste the SHA-256 recorded at acquisition (a hash, sha256sum or BSD lines): each
        evidence file says match, MISMATCH or not hashed yet."""
        dlg = tk.Toplevel(parent)
        dlg.title("Compare with expected hash")
        dlg.configure(bg=C["bg"])
        dlg.transient(parent)
        ttk.Label(dlg, text="Paste the expected SHA-256: one hash (compared with the database "
                            "file), or lines like '<hash>  <file name>' (sha256sum) or 'SHA256 "
                            "(<file name>) = <hash>'.", wraplength=520,
                  justify="left").pack(fill="x", padx=10, pady=(10, 4))
        box = tk.Text(dlg, width=72, height=7, font=F["mono"])
        box.pack(fill="both", expand=True, padx=10)
        out = ttk.Label(dlg, text="", wraplength=520, justify="left")
        out.pack(fill="x", padx=10, pady=4)

        def check():
            result = member.db.evidence.compare_expected(box.get("1.0", "end"))
            lines = ["%s: %s" % (os.path.basename(r["path"]), r["result"]) for r in result]
            lines += ["listed but not an evidence file: %s" % n for n in result.unmatched]
            lines += ["not read: %r (%s)" % (ln[:40], why) for ln, why in result.unparsed]
            out.configure(text="\n".join(lines) or "Nothing to compare.")
            self.activity("verify", database=member.path, expected_hash=[
                (r["path"], r["result"]) for r in result])
        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Compare", style="P.TButton", command=check).pack(side="left")
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side="right")
        return dlg

    # ── The activity log window ──────────────────────────────────────
    def _show_activity(self):
        """What was done with this case (opened with hashes, searches, exports with their
        paths, verification), kept in the app-data folder; viewable and exportable."""
        log = self._activity_log
        if log is None:
            messagebox.showinfo("Activity log", "No database loaded.", parent=self)
            return
        from engine.activity import entry_text
        # The log is append-only and unbounded over the tool's lifetime; never read
        # the whole file into the window.
        entries = log.entries(limit=2000)
        win = tk.Toplevel(self)
        win.title("Activity log (last %d entries)" % len(entries))
        fit_geometry(win, 900, 460)
        win.configure(bg=C["bg"])
        head = ttk.Label(win, text="Kept in %s%s" % (log.path, (" — NOT written: %s" % log.error)
                                                    if log.error else ""),
                         style="M.TLabel", wraplength=860, justify="left")
        head.pack(fill="x", padx=8, pady=(8, 2))
        search = SearchBox(win, placeholder="Find in the log…", primary=True, width=30)
        search.pack(fill="x", padx=8, pady=(0, 4))
        txt = tk.Text(win, font=F["mono"], wrap="none", bg=C["bg2"], relief="flat")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=8, pady=8)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=(8, 0))
        txt.insert("1.0", "\n".join(entry_text(e) for e in entries) or "Nothing yet.")
        txt.configure(state="disabled")
        win.finder = TextFind(txt, search)

        def export():
            opts = export_options(win, "Export the activity log", [],
                                  formats=("csv", "json", "txt"), blobs=False)
            if opts is None:
                return
            path = ask_path(win, opts["fmt"], "activity_log")
            if not write_allowed(path):
                return
            try:
                n = log.export(path, opts["fmt"])
            except Exception as e:      # noqa: BLE001 - shown to the user
                messagebox.showerror("Activity log", str(e), parent=win)
                return
            messagebox.showinfo("Activity log", "%d entries written to:\n%s" % (n, path),
                                parent=win)
        ttk.Button(bar, text="Export…", command=export).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        return win

    def _update_activity_log(self):
        """The case changed: its activity log (same databases, same file in app data); the
        databases now in it are noted as opened, with what is known of their evidence."""
        paths = [m.path for m in self.case]
        if not paths:
            self._activity_log = None
            return
        from engine.activity import ActivityLog
        try:
            log = ActivityLog(paths, refuse_in=self.case.evidence_dirs())
        except Exception:               # noqa: BLE001 - the log never stops the work
            self._activity_log = None
            return
        old = self._activity_log
        self._activity_log = log
        if old is not None and old.path == log.path:
            return
        self._activity_hashes = set()
        items = []
        for m in self.case:
            if not m.db.ok:
                continue
            files = [dict((k, fp[k]) for k in ("role", "path", "size", "mtime_ns", "sha256"))
                     for fp in m.db.evidence.summary()]
            items.append(("open", dict(database=m.path, mode=m.db.mode, files=files,
                                       tool="SQLite GUI Analyzer %s" % VERSION)))
        self.activity_many(items)

    def _log_new_hashes(self):
        """Note each database's SHA-256 in the activity log once its hashing finished."""
        if self._activity_log is None:
            return
        seen = getattr(self, "_activity_hashes", None)
        if seen is None:
            seen = self._activity_hashes = set()
        items = []
        for m in self.case:
            if m.path in seen or not m.db.ok or not m.db.evidence.hashing_done:
                continue
            seen.add(m.path)
            items.append(("hash", dict(database=m.path, files=[
                (fp["role"], fp["path"], fp["sha256"]) for fp in m.db.evidence.summary()])))
        self.activity_many(items)

    # ═══════════════════════════════════════════════════════════════════════
    # ── SQL QUERY EDITOR TAB ─────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def _build_sql_tab(self):
        sf = self._sql_frame

        hdr = ttk.Frame(sf)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(hdr, text="SQL Query Editor", style="B.TLabel").pack(side="left")
        self._sql_rules = wrap_to_width(ttk.Label(
            sf, text="Read-only: SELECT, WITH, VALUES, EXPLAIN and read-only PRAGMA statements "
                     "run (comments first are fine); a statement that would change the database "
                     "is refused.", style="M.TLabel"))
        self._sql_rules.pack(fill="x", padx=10)
        # what SQL cannot see (committed WAL frames in main-only mode, or no SQL at all)
        self._sql_notice = ttk.Frame(sf)
        # one line (the reason in its Details), never a paragraph above the editor
        self._sql_notice_lbl = StatusLine(self._sql_notice, style="Warning.TLabel")
        self._sql_notice_lbl.pack(side="left", fill="x", expand=True, padx=(4, 4), pady=3)
        ttk.Button(self._sql_notice, text="Limits…",
                   command=lambda: self.datamap.limits_window(self)).pack(side="right", padx=4)
        ttk.Button(self._sql_notice, text="Browse (WAL applied)",
                   command=lambda: self._nb.select(self._browse_frame)).pack(side="right",
                                                                              padx=4)

        editor_outer = tk.Frame(sf, relief="solid", bd=1, bg=C["border"])
        editor_outer.pack(fill="x", padx=10, pady=(2, 0))

        self._sql_editor = tk.Text(
            editor_outer, height=8, font=F["mono_large"],
            bg=C["bg2"], fg=C["text"], insertbackground=C["text"],
            wrap="none", undo=True, relief="flat", padx=6, pady=4,
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=K["ring"],
        )
        sql_xsb = ttk.Scrollbar(editor_outer, orient="horizontal",
                                command=self._sql_editor.xview)
        self._sql_editor.configure(xscrollcommand=sql_xsb.set)
        sql_xsb.pack(side="bottom", fill="x")
        self._sql_editor.pack(fill="both", expand=True)

        self._sql_editor.tag_configure("kw", foreground=K["primary"], font=F["mono_bold"])
        self._sql_editor.tag_configure("str", foreground=K["success_text"])
        self._sql_editor.tag_configure("cmt", foreground=K["placeholder"], font=F["mono_italic"])
        self._sql_editor.tag_configure("num", foreground=K["warning"])

        self._sql_editor.bind("<KeyRelease>",     self._sql_on_key)
        self._sql_editor.bind("<Control-Return>",
            lambda e: (self._sql_run(), "break")[1])
        self._sql_editor.bind("<Tab>",
            lambda e: (self._sql_editor.insert("insert", "    "), "break")[1])
        self._sql_editor.bind("<Alt-Up>",
            lambda e: (self._sql_history_prev(), "break")[1])
        self._sql_editor.bind("<Alt-Down>",
            lambda e: (self._sql_history_next(), "break")[1])

        tbar = Toolbar(sf)
        tbar.pack(fill="x", padx=M, pady=(XS, XS))

        # one primary action (Run), then the editor's own actions, then the results'
        self._sql_run_btn = ttk.Button(tbar, text="▶  Run", style="Primary.TButton",
                                       command=self._sql_run)
        tbar.add(self._sql_run_btn)
        self._sql_cancel_btn = ttk.Button(tbar, text="■  Stop", command=self._sql_cancel)
        tbar.add(self._sql_cancel_btn, visible=False)
        self._sql_tbar = tbar
        tbar.group()
        for label, cmd in [
            ("Clear",    lambda: self._sql_editor.delete("1.0", "end")),
            ("Copy SQL", self._sql_copy_query),
        ]:
            tbar.add(ttk.Button(tbar, text=label, style="Subtle.TButton", command=cmd))
        tbar.group()
        # one Export ▾, as on the other tabs (enabled once a query returned rows)
        self._sql_export_menu = tk.Menu(self, tearoff=0)
        self._sql_export_menu.add_command(label="Export CSV…", command=self._sql_export_csv)
        self._sql_export_menu.add_command(label="Export JSON…", command=self._sql_export_json)
        self._sql_export_btn = ttk.Button(
            tbar, text="Export ▾", state="disabled",
            command=lambda: self._post_menu(self._sql_export_menu, self._sql_export_btn))
        tbar.add(self._sql_export_btn)
        self._sql_export_json_btn = self._sql_export_btn    # one button for both formats

        tbar.add(ttk.Label(tbar, text="Limit", style="Muted.TLabel"), gap=M)
        self._sql_limit_var = tk.StringVar(value="1000")
        sql_lim_cb = SearchableCombobox(
            tbar, textvariable=self._sql_limit_var,
            values=["100", "500", "1000", "5000", "All"],
            state="readonly", width=7,
        )
        tbar.add(sql_lim_cb, gap=2)
        ToolTip(sql_lim_cb, "Maximum rows to return")

        sbar = ttk.Frame(sf)
        sbar.pack(fill="x", padx=10, pady=(0, 2))
        self._sql_col_info = ttk.Label(sbar, text="", style="M.TLabel")
        self._sql_col_info.pack(side="right")
        self._sql_status_label = wrap_to_width(ttk.Label(
            sbar, text="Open a database to start querying.", style="M.TLabel"), pad=90)
        self._sql_status_label.pack(side="left", fill="x", expand=True)

        self._sql_pw = ttk.PanedWindow(sf, orient="vertical")
        self._sql_pw.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        res_outer = ttk.Frame(self._sql_pw)
        self._sql_pw.add(res_outer, weight=4)
        res_border = tk.Frame(res_outer, relief="solid", bd=1, bg=C["border"])
        res_border.pack(fill="both", expand=True)

        # Results in the same virtual grid as Browse (rows held in memory: sorting and the
        # column filters apply to the rows the query returned)
        self._sql_grid = DataGrid(res_border, frozen=0, on_open_row=self._sql_open_row,
                                  on_open_blob=lambda row, column, value: BlobViewer(
                                      self, value, column, "%s.%s — row %s" % (
                                          self._sql_grid.context(), column,
                                          format(row + 1, ","))),
                                  on_filter=lambda _e, _g: self._sql_update_count(),
                                  describe=self._describe_value)
        self._sql_grid.context = lambda: "query result, %s" % self.case.active.name \
            if self.case.active is not None else "query result"
        self._sql_search = grid_search(res_outer, self._sql_grid,
                                       placeholder="Find in the results (words, all must "
                                                   "match)…")
        self._sql_search.pack(fill="x", before=res_border, pady=(0, 2))
        self._sql_grid.pack(fill="both", expand=True)

        hist_outer = ttk.Frame(self._sql_pw)
        self._sql_pw.add(hist_outer, weight=1)

        hist_hdr = ttk.Frame(hist_outer)
        hist_hdr.pack(fill="x")
        ttk.Label(hist_hdr, text="Query history", style="B.TLabel").pack(
            side="left", padx=4, pady=(4, 2))
        self._sql_hist_note = ttk.Label(
            hist_hdr, text="(Alt+↑/↓ in the editor; the last %d queries are kept: limit "
                           "sql_history)" % limits.get("sql_history"), style="M.TLabel")
        self._sql_hist_note.pack(side="left")
        tk.Button(
            hist_hdr, text="Clear history",
            font=F["small"], bg=C["bg3"], fg=C["text2"],
            activebackground=C["bg4"], relief="flat", bd=0,
            padx=6, pady=2, cursor="hand2",
            command=self._sql_clear_history,
        ).pack(side="right", padx=4)

        hist_border = tk.Frame(hist_outer, relief="solid", bd=1, bg=C["border"])
        hist_border.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._sql_hist_listbox = tk.Listbox(
            hist_border, font=F["mono"],
            bg=C["bg2"], fg=C["text"],
            selectbackground=C["tsel"], selectforeground=C["text"],
            relief="flat", activestyle="none",
        )
        hist_vsb = ttk.Scrollbar(
            hist_border, orient="vertical",
            command=self._sql_hist_listbox.yview)
        self._sql_hist_listbox.configure(yscrollcommand=hist_vsb.set)
        hist_vsb.pack(side="right", fill="y")
        self._sql_hist_listbox.pack(fill="both", expand=True)
        self._sql_hist_listbox.bind("<Double-1>", self._sql_hist_load)
        self._sql_hist_listbox.bind("<Return>",   self._sql_hist_load)
        ToolTip(self._sql_hist_listbox, "Double-click to restore query in editor")

    _SQL_KEYWORDS = frozenset({
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL",
        "LIKE", "GLOB", "BETWEEN", "JOIN", "LEFT", "INNER", "OUTER", "CROSS",
        "ON", "AS", "ORDER", "BY", "ASC", "DESC", "GROUP", "HAVING", "LIMIT",
        "OFFSET", "DISTINCT", "ALL", "UNION", "EXCEPT", "INTERSECT", "WITH",
        "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "EXISTS", "EXPLAIN",
        "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IFNULL", "NULLIF",
        "SUBSTR", "LENGTH", "TRIM", "UPPER", "LOWER", "REPLACE", "TYPEOF",
        "DATE", "TIME", "DATETIME", "STRFTIME", "JULIANDAY", "ROWID",
    })

    def _sql_highlight(self, event=None):
        try:
            txt = self._sql_editor
            for tag in ("kw", "str", "cmt", "num"):
                txt.tag_remove(tag, "1.0", "end")
            code = txt.get("1.0", "end")
            for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", code):
                if m.group(1).upper() in self._SQL_KEYWORDS:
                    txt.tag_add("kw", "1.0+{}c".format(m.start()),
                                "1.0+{}c".format(m.end()))
            for m in re.finditer(r"'[^']*'|\"[^\"]*\"", code):
                txt.tag_add("str", "1.0+{}c".format(m.start()),
                            "1.0+{}c".format(m.end()))
            for m in re.finditer(r"--[^\n]*", code):
                txt.tag_add("cmt", "1.0+{}c".format(m.start()),
                            "1.0+{}c".format(m.end()))
            for m in re.finditer(r"\b\d+\.?\d*\b", code):
                txt.tag_add("num", "1.0+{}c".format(m.start()),
                            "1.0+{}c".format(m.end()))
        except Exception:
            pass

    def _sql_on_key(self, event=None):
        if hasattr(self, "_sql_hl_after"):
            self.after_cancel(self._sql_hl_after)
        self._sql_hl_after = self.after(150, self._sql_highlight)

    def _focus_sql_editor(self):
        self._nb.select(self._sql_frame)
        if hasattr(self, "_sql_editor"):
            self._sql_editor.focus_set()

    def _sql_copy_query(self):
        sql = self._sql_editor.get("1.0", "end-1c").strip()
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)

    def _sql_clear_history(self):
        self._sql_query_history.clear()
        self._sql_query_history_idx = -1
        if hasattr(self, "_sql_hist_listbox"):
            self._sql_hist_listbox.delete(0, "end")

    def _sql_history_prev(self):
        if not self._sql_query_history:
            return
        idx = max(0, self._sql_query_history_idx - 1)
        self._sql_query_history_idx = idx
        self._sql_editor.delete("1.0", "end")
        self._sql_editor.insert("1.0", self._sql_query_history[idx])

    def _sql_history_next(self):
        if not self._sql_query_history:
            return
        idx = min(len(self._sql_query_history) - 1,
                self._sql_query_history_idx + 1)
        self._sql_query_history_idx = idx
        self._sql_editor.delete("1.0", "end")
        self._sql_editor.insert("1.0", self._sql_query_history[idx])

    def _sql_hist_load(self, event=None):
        sel = self._sql_hist_listbox.curselection()
        if not sel:
            return
        # Listbox is prepended so index 0 = most recent
        real_idx = len(self._sql_query_history) - 1 - sel[0]
        if 0 <= real_idx < len(self._sql_query_history):
            self._sql_editor.delete("1.0", "end")
            self._sql_editor.insert("1.0", self._sql_query_history[real_idx])
            self._nb.select(self._sql_frame)
            self._sql_editor.focus_set()

    def _sql_is_safe(self, sql):
        """A statement that reads (after any comments): the connection refuses writes anyway."""
        return sql_reads_only(sql)

    def _update_sql_notice(self):
        """Say above the results what SQL cannot see: committed WAL frames when the database is
        read in main-only mode (the RAM limit or the Python version), or everything when SQLite
        cannot read the file. Hidden when SQL sees the current state."""
        text, details = "", []
        s = self.db.session if self.db.ok else None
        if s is not None and s.sql is None:
            text = "⚠ SQL is unavailable: SQLite cannot read this file"
            details = ["SQLite: %s" % (s.sql_error or "unknown error"),
                       "Browse and Search show the rows the tool parsed itself."]
        elif s is not None and not s.sql_sees_current_state and s.wal is not None:
            n = s.wal.last_commit + 1
            text = ("⚠ SQL sees the main file only: %s committed WAL frame%s are NOT in these "
                    "results" % (format(n, ","), "" if n == 1 else "s"))
            details = ["Browse, Search and the Timeline include them.", s.main_only_reason()]
        self._sql_notice_lbl.set(text, details)
        if text and not self._sql_notice.winfo_manager():
            self._sql_notice.pack(fill="x", padx=10, pady=(2, 0), after=self._sql_rules)
        elif not text and self._sql_notice.winfo_manager():
            self._sql_notice.pack_forget()

    def _sql_clear_results(self):
        """No rows under a statement that was not run (the previous results go)."""
        self._sql_grid.set_source(None)
        self._sql_result_rows, self._sql_result_cols = [], []
        self._sql_col_info.configure(text="")
        self._sql_export_btn.configure(state="disabled")
        self._sql_export_json_btn.configure(state="disabled")

    def _sql_run(self):
        if not self.db.ok:
            messagebox.showwarning("No database",
                                "Please open a database first.")
            return
        sql = self._sql_editor.get("1.0", "end-1c").strip()
        if not sql:
            self._sql_clear_results()
            self._sql_status_label.configure(text="Nothing to run: the editor is empty.")
            return
        if not self._sql_is_safe(sql):
            word = sql_first_keyword(sql) or "(nothing)"
            self._sql_clear_results()   # never leave an earlier query's rows under this
            self._sql_status_label.configure(
                text="✖  Not run: %s changes or is not a statement that reads. Allowed: SELECT, "
                     "WITH, VALUES, EXPLAIN and read-only PRAGMA (the database is only read)."
                     % word)
            return
        self._sql_last = sql
        if not self._sql_query_history or self._sql_query_history[-1] != sql:
            self._sql_query_history.append(sql)
            self._sql_query_history_idx = len(self._sql_query_history) - 1
            display = sql[:90].replace("\n", " ")
            self._sql_hist_listbox.insert(0, display)
            keep = limits.get("sql_history")
            while self._sql_hist_listbox.size() > keep:
                self._sql_hist_listbox.delete("end")
                self._sql_query_history.pop(0)

        lim_str = self._sql_limit_var.get()
        limit = None if lim_str == "All" else int(lim_str)

        self._sql_grid.set_source(None)
        self._sql_result_rows = []
        self._sql_result_cols = []
        self._sql_status_label.configure(text="Running…")
        self._sql_col_info.configure(text="")
        self._sql_run_btn.configure(state="disabled")
        self._sql_tbar.show(self._sql_cancel_btn, True)
        self._sql_export_btn.configure(state="disabled")
        self._sql_export_json_btn.configure(state="disabled")
        self._sql_query_cancel = False

        def _worker():
            conn = self.db.new_sql_conn()
            self._sql_conn = conn            # _sql_cancel interrupts it
            try:
                if conn is None:
                    raise RuntimeError("SQL is unavailable: SQLite cannot read this file "
                                       "(the tool is showing natively parsed data).")
                cur  = conn.execute(sql)
                cols = [d[0] for d in (cur.description or [])]
                rows = []
                more = False
                for row in cur:
                    if self._sql_query_cancel:
                        break
                    if limit and len(rows) >= limit:
                        more = True          # one row past the limit: there are more
                        break
                    rows.append(tuple(row))
                if self._sql_query_cancel:
                    return                   # the status already says "Stopped."
                self._after_safe(0, lambda: self._sql_show_results(cols, rows, limit if more else None))
            except Exception as exc:
                if self._sql_query_cancel:
                    return                   # interrupted by Cancel (or by closing the DB)
                err = str(exc)
                self._after_safe(0, lambda: self._sql_show_error(err))
            finally:
                if self._sql_conn is conn:
                    self._sql_conn = None
                if conn is not None:
                    self.db.release_sql_conn(conn)

        self._sql_query_thread = threading.Thread(target=_worker, daemon=True)
        self._sql_query_thread.start()

    def _sql_cancel(self):
        self._sql_query_cancel = True
        conn = self._sql_conn
        if conn is not None:
            try:
                conn.interrupt()             # stop the statement now, not at the next row
            except sqlite3.Error:
                pass                         # it already finished and was closed
        self._sql_run_btn.configure(state="normal")
        self._sql_tbar.show(self._sql_cancel_btn, False)
        self._sql_status_label.configure(text="Stopped.")

    def _sql_show_results(self, cols, rows, limit):
        self._sql_run_btn.configure(state="normal")
        self._sql_tbar.show(self._sql_cancel_btn, False)
        self._sql_result_cols = cols
        self._sql_result_rows = rows
        if not cols:
            self._sql_grid.set_source(None)
            self._sql_status_label.configure(
                text="Statement executed. No rows returned.")
            return
        self._sql_limited = bool(limit)          # a row past the limit was there
        self._sql_grid.set_source(ListSource(cols, [(row, ()) for row in rows],
                                             encoding=self.db.encoding))
        self._sql_update_count()
        self._sql_col_info.configure(text="{} column{}".format(
            len(cols), "s" if len(cols) != 1 else ""))
        if rows:
            self._sql_export_btn.configure(state="normal")
            self._sql_export_json_btn.configure(state="normal")

    def _sql_update_count(self):
        """Status line of the SQL tab: rows returned, and how many the column filters keep."""
        src = self._sql_grid.source
        if src is None:
            return
        status = "{:,} row{} returned".format(src.total, "s" if src.total != 1 else "")
        if src.filtered:
            status += "  ({:,} kept by the column filters)".format(src.row_count())
        if self._sql_limited:
            status += "  (stopped at the Limit: the query has more rows; choose a larger " \
                      "Limit or All)"
        self._sql_status_label.configure(text=status)

    def _sql_view_rows(self):
        """The result rows as the grid shows them (column filters and sort applied)."""
        src = self._sql_grid.source
        return list(src.iter_rows()) if src is not None else list(self._sql_result_rows)

    def _sql_show_error(self, err):
        self._sql_run_btn.configure(state="normal")
        self._sql_tbar.show(self._sql_cancel_btn, False)
        self._sql_status_label.configure(
            text="✖  Error: {}".format(err[:300]))
        self._sql_col_info.configure(text="")

    def _sql_open_row(self, _row, vals):
        """Row detail of a query result (double-click or Enter in the results grid): each
        column's value and storage class, BLOBs in the BLOB Inspector, lossless copies."""
        if not vals:
            return None
        s = self.db.session
        seen = "" if s is None or s.sql_sees_current_state else \
            " (SQL sees the main file only: committed WAL frames not included)"
        return ValuesWindow(self, "Row detail — query result",
                            "Query result of %s%s:\n%s" % (
                                self.member_label(self.case.active, None) or "the database",
                                seen, getattr(self, "_sql_last", "")[:300]),
                            self._sql_result_cols, list(vals))

    def _sql_export(self, fmt):
        """Export the query's rows (as the grid shows them: column filters and sort applied)
        on a worker thread, with the query, and what SQL could see, in the provenance."""
        if not self._sql_result_rows:
            return
        src = self._sql_grid.source
        n = src.row_count() if src is not None else len(self._sql_result_rows)
        opts = export_options(self, "Export query results",
                              [("rows", "The %s shown%s" % (
                                  plural(n, "row"), " (column filters applied)"
                                  if src is not None and src.filtered else ""))], fmt=fmt,
                              spreadsheet_safe=True)
        if opts is None:
            return
        fmt = opts["fmt"]
        path = ask_path(self, fmt, "query_results")
        if not write_allowed(path):
            return
        rows = self._sql_view_rows()
        cols = list(self._sql_result_cols)
        s = self.db.session
        seen = ("the current state (WAL applied)" if s.sql_sees_current_state
                else "the main file only: committed WAL frames NOT included")
        extra = {"sql": getattr(self, "_sql_last", ""), "sql_sees": seen,
                 "stopped_at_limit": bool(self._sql_limited)}
        export_rows(self, "Export query results", path, fmt, cols, lambda: iter(rows),
                    "SQL query results", [self.case.active], scope=plural(n, "row"),
                    filters="; ".join("%s: %s" % kv for kv in sorted(
                        self._sql_grid.filter_texts().items())),
                    blob_mode=opts["blob_mode"], total=len(rows), extra=extra,
                    spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def _sql_export_csv(self):
        self._sql_export("csv")

    def _sql_export_json(self):
        self._sql_export("json")


    def _wal_row_tag(self, result):
        """Treeview tag for a search result row (WAL results are coloured by frame state)."""
        if result.get("source", "DB").startswith("WAL"):
            state = result.get("category", "")
            if state in WAL_STATES:
                return "wal_" + state
        return None

    def _configure_result_tags(self, tree):
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        for state, (_label, _fg, bg, _desc) in WAL_STATES.items():
            tree.tag_configure("wal_" + state, background=bg)
        tree.tag_configure("sr_child", foreground=C["text2"])   # a row's cells / frames
