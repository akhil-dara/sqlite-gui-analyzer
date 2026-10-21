"""The UI of a case of several databases (case.Case).

member_tooltip   the hover card text of a database: path, size, SHA-256, how it was opened.
OpenFolderDialog, in three steps: 1 the folder (and its subfolders or not); 2 which
                 files, optional: conditions of the user's own (file name / path / folder /
                 extension / size, all or any of them: engine.filematch); 3 Scan for
                 databases - a line says beforehand what the scan will do, files that do
                 not match the conditions are never opened, and the scan shows files seen
                 and databases found with Stop. Then the list (header checked, size, WAL /
                 journal, relative path; all ticked; the conditions and a find box filter
                 it live; Select all / none, Tick shown only; columns sort on a click) and
                 Open N databases. A folder given by the caller is scanned at once.
(The databases are listed by navigator.CaseNavigator; which ones each feature covers is
scope.ScopePicker; the tables a search covers is dialogs.ScopeDlg.for_case.)
"""

import os
import threading
from collections import OrderedDict
import tkinter as tk
from tkinter import filedialog, ttk

from constants import C
from combobox import SearchableCombobox
from engine import filematch
from engine.case import scan_folder
from utils import fmtb
from widgets import SearchBox, fit_geometry, next_line

CHECK, UNCHECK = "☑", "☐"


def _hash_text(member):
    ev = member.db.evidence if member.db.ok else None
    if ev is None:
        return "-"
    sha = member.sha256()
    if sha:
        return sha
    if ev.hash_error:
        return "not hashed: %s" % ev.hash_error
    return "hashing… %d%%" % (100 * ev.hashed_bytes // max(ev.total_bytes, 1))


def member_tooltip(member, active=False):
    lines = ["%s%s" % (member.name, "  (active database)" if active else ""),
             member.path,
             "Size: %s" % (fmtb(member.size) if member.size is not None else "?"),
             "SHA-256: %s" % _hash_text(member),
             "Opened: %s" % member.status()]
    if member.changes:
        lines.append("CHANGED since the case was saved: %s" % "; ".join(member.changes))
    lines.append("Click: make active.  Right-click: more.")
    return "\n".join(lines)


class OpenFolderDialog(tk.Toplevel):
    """Choose the SQLite databases of a folder to open. result: the chosen paths, or None."""

    def __init__(self, parent, folder=None, recursive=True, title="Open Folder",
                 auto_choose=True):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(parent)
        fit_geometry(self, 900, 540)
        self.result = None
        self.evidence_folders = []     # set by ok(): folders the write guard keeps out of
        self.candidates = []
        self.ticked = set()             # paths
        self._cancel = [False]
        self._thread = None
        self._progress = [0]
        self._found = None
        self.scanning = False

        # 1 the folder
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="1  Folder", style="Heading.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(top, text="Choose folder…", command=self.choose_folder).pack(side="left")
        self.folder_var = tk.StringVar(value=folder or "")
        ttk.Label(top, textvariable=self.folder_var, style="M.TLabel").pack(side="left", padx=6)
        self.recursive_var = tk.BooleanVar(value=recursive)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var,
                        command=self._settings_changed).pack(side="right")
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8, side="bottom")
        # 2 which files (optional)
        self._build_conditions()
        # 3 scan
        self._build_scan_row()
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(4, 2))
        # the databases found
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=2)
        self.search = SearchBox(bar, placeholder="Find a file…", delay=0, find_button=False,
                                primary=True, width=24, on_change=lambda t: self.fill(),
                                on_next=lambda forward: next_line(self.tree, forward))
        self.search.pack(side="left", padx=(0, 4))
        self.filter_var = self.search.var
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="left",
                                                                                  padx=2)
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(
            side="left", padx=2)
        ttk.Button(bar, text="Tick shown only", command=self.tick_shown_only).pack(
            side="left", padx=2)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        cols = (("on", "", 30), ("name", "Name", 180), ("size", "Size", 90),
                ("side", "WAL / journal", 160), ("rel", "Relative path", 380))
        self.tree = ttk.Treeview(box, columns=[c for c, _t, _w in cols], show="headings",
                                 selectmode="extended")
        self._sort = (None, False)
        self._headings = dict((c, t) for c, t, _w in cols)
        for c, t, w in cols:
            self.tree.heading(c, text=t, command=(lambda c=c: self.sort_by(c)) if c != "on"
                              else "")
            self.tree.column(c, width=w, stretch=c == "rel", anchor="e" if c == "size" else "w")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        # empty state: until a scan has run the list says what to do, not a blank table
        self.empty = ttk.Frame(box, style="Card.TFrame")
        self.empty_title = ttk.Label(self.empty, text="", style="CardHeading.TLabel")
        self.empty_title.pack(pady=(0, 4))
        self.empty_text = ttk.Label(self.empty, text="", style="CardMuted.TLabel",
                                    wraplength=420, justify="center")
        self.empty_text.pack()
        self.empty_btn = ttk.Button(self.empty, text="Choose folder…", style="P.TButton",
                                    command=self._empty_action)
        self.empty_btn.pack(pady=(10, 0))
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w")
        self.status.pack(fill="x", padx=10, side="bottom", after=bot)
        ttk.Button(bot, text="Cancel", command=self.close).pack(side="right")
        self.open_btn = ttk.Button(bot, text="Open", style="P.TButton", command=self.ok)
        self.open_btn.pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self._update_plan()
        if folder:
            self.rescan()
        elif auto_choose:
            # opened from the Open menu with no folder: ask for it at once, instead
            # of showing an empty list first (tests pass auto_choose=False)
            self.after(30, self._auto_choose)
        self._update_status()

    def _auto_choose(self):
        """Pick the folder right away; a cancelled picker closes the dialog."""
        try:
            if not self.winfo_exists() or self.folder_var.get():
                return
            self.choose_folder()
            if not self.folder_var.get():
                self.close()
        except tk.TclError:
            pass

    # -- conditions ----------------------------------------------------------------------
    def _build_conditions(self):
        """Step 2: conditions on the files (optional): rows of field, operator, value; 'Match
        all / any of them' once there are two or more."""
        frame = self.cond_frame = ttk.Frame(self)
        frame.pack(fill="x", padx=8, pady=(6, 0))
        head = ttk.Frame(frame)
        head.pack(fill="x")
        ttk.Label(head, text="2  Which files", style="Heading.TLabel").pack(side="left", padx=(0, 8))
        self.cond_hint = ttk.Label(head, text="every SQLite file (optional: add conditions "
                                             "to look for particular files)",
                                   style="M.TLabel")
        self.cond_hint.pack(side="left")
        ttk.Button(head, text="+ Add a condition", command=self.add_condition).pack(
            side="left", padx=8)
        self.mode_frame = ttk.Frame(head)
        ttk.Label(self.mode_frame, text="Match").pack(side="left")
        self.mode_var = tk.StringVar(value="all")
        mode = SearchableCombobox(self.mode_frame, textvariable=self.mode_var,
                                  state="readonly", width=5,
                                  values=[m for m, _l in filematch.MODES])
        mode.pack(side="left", padx=4)
        mode.bind("<<ComboboxSelected>>", lambda e: self._conditions_changed())
        ttk.Label(self.mode_frame, text="of them").pack(side="left")
        self.rows_frame = ttk.Frame(frame)       # packed while there are conditions
        self.conditions = []        # [(field var, op var, value var, row frame, op combo)]
        self._cond_after = None

    def _build_scan_row(self):
        """Step 3: Scan for databases, Stop while it runs, and a line saying what the scan
        will do (or is doing, or did)."""
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(row, text="3", style="Heading.TLabel").pack(side="left", padx=(0, 8))
        self.scan_btn = ttk.Button(row, text="Scan for databases", style="P.TButton",
                                   command=self.rescan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text="Stop", style="D.TButton", command=self.stop)
        # the bar and Stop are shown only while a scan runs (nothing looks busy at idle)
        self.bar = ttk.Progressbar(row, mode="indeterminate", length=120)
        self.plan = ttk.Label(row, text="", style="M.TLabel", anchor="w", wraplength=560,
                              justify="left")
        self.plan.pack(side="left", padx=8, fill="x", expand=True)
        self._scanned_key = None    # (folder, subfolders, conditions) of the last scan

    def add_condition(self, field="name", op="equals", value=""):
        """One more condition row (field, operator, value, ×); returns its variables."""
        if not self.rows_frame.winfo_manager():
            self.rows_frame.pack(fill="x")
        row = ttk.Frame(self.rows_frame)
        row.pack(fill="x", pady=1)
        labels = dict(filematch.FIELDS)
        fvar = tk.StringVar(value=labels.get(field, labels["name"]))
        fcombo = SearchableCombobox(row, textvariable=fvar, state="readonly", width=18,
                              values=[lb for _k, lb in filematch.FIELDS])
        fcombo.pack(side="left")
        ovar = tk.StringVar()
        ocombo = SearchableCombobox(row, textvariable=ovar, state="readonly", width=18)
        ocombo.pack(side="left", padx=4)
        vvar = tk.StringVar(value=value)
        entry = ttk.Entry(row, textvariable=vvar, width=36)
        entry.pack(side="left", fill="x", expand=True)
        ent = [fvar, ovar, vvar, row, ocombo]

        def set_ops(keep=None):
            key = self._field_key(fvar.get())
            ops = filematch.SIZE_OPS if key == "size" else filematch.TEXT_OPS
            ocombo.configure(values=[lb for _k, lb in ops])
            names = dict(ops)
            ovar.set(names.get(keep) or ops[0][1])

        def remove():
            row.destroy()
            if ent in self.conditions:
                self.conditions.remove(ent)
            if not self.conditions:
                self.rows_frame.pack_forget()
            self._conditions_changed()
            self._update_plan()
        ttk.Button(row, text="×", width=3, command=remove).pack(side="left", padx=4)
        set_ops(op)
        fcombo.bind("<<ComboboxSelected>>", lambda e: (set_ops(), self._conditions_changed()))
        ocombo.bind("<<ComboboxSelected>>", lambda e: self._conditions_changed())
        vvar.trace_add("write", lambda *_a: self._conditions_changed())
        self.conditions.append(ent)
        entry.focus_set()
        self._conditions_changed()
        self._update_plan()
        return ent

    @staticmethod
    def _field_key(label):
        for k, lb in filematch.FIELDS:
            if lb == label or k == label:
                return k
        return "name"

    @staticmethod
    def _op_key(label):
        for k, lb in filematch.TEXT_OPS + filematch.SIZE_OPS:
            if lb == label or k == label:
                return k
        return "equals"

    def condition_list(self):
        """The conditions as engine.filematch.Condition objects (incomplete ones included:
        matches() leaves them out)."""
        return [filematch.Condition(self._field_key(f.get()), self._op_key(o.get()), v.get())
                for f, o, v, _r, _c in self.conditions]

    def _accept(self):
        """The scan's file test from the conditions (None: every file is looked at)."""
        conds = [c for c in self.condition_list() if c.complete()]
        if not conds:
            return None
        mode = self.mode_var.get()
        return lambda name, rel, size: filematch.matches(conds, mode, name, rel, size)

    def _conditions_changed(self):
        if getattr(self, "_closing", False):
            return                      # (a variable written while the window goes)
        if self._cond_after is not None:
            try:
                self.after_cancel(self._cond_after)
            except tk.TclError:
                pass
        try:
            self._cond_after = self.after(150, self._apply_conditions)
        except tk.TclError:
            self._cond_after = None

    def _apply_conditions(self):
        pending, self._cond_after = self._cond_after, None
        if pending is not None:
            try:
                self.after_cancel(pending)      # (run now: the one waiting is not needed)
            except tk.TclError:
                pass
        self.fill()
        self._update_plan()

    # -- what the scan will do ---------------------------------------------------------------
    def _scan_key(self):
        conds = filematch.describe(self.condition_list(), self.mode_var.get())
        return (self.folder_var.get(), bool(self.recursive_var.get()), conds)

    def _settings_changed(self):
        """Include subfolders changed: said; the next scan uses it (nothing starts by itself)."""
        self._update_plan()
        self._update_status()

    def _update_plan(self):
        """The line beside Scan: what the scan will do, or that the list is from an earlier
        scan with other settings; and the 'Match all / any' choice once there are two
        conditions or more."""
        if not hasattr(self, "plan"):
            return
        complete = [c for c in self.condition_list() if c.complete()]
        if len(self.conditions) >= 2:
            if not self.mode_frame.winfo_manager():
                self.mode_frame.pack(side="left", padx=4)
        elif self.mode_frame.winfo_manager():
            self.mode_frame.pack_forget()
        self.cond_hint.configure(text="every SQLite file (optional: add conditions to look "
                                      "for particular files)" if not self.conditions else
                                 "the files where:")
        if self.scanning:
            return                      # _poll writes the progress there
        folder = self.folder_var.get()
        where = "this folder and its subfolders" if self.recursive_var.get() else \
            "this folder only (not its subfolders)"
        conds = filematch.describe(complete, self.mode_var.get())
        if not folder:
            text = "Choose a folder first."
        elif conds:
            text = ("Will look in %s and open only the files where %s (the others are not "
                    "opened at all)." % (where, conds))
        else:
            text = ("Will look in %s at every file and list those that start with the "
                    "SQLite header, whatever their name." % where)
        if self._scanned_key is not None and folder and getattr(self, "_stopped", False):
            st = getattr(self, "_stats", None) or {}
            text = ("Scan stopped after %s files seen: the list holds the %s database%s found "
                    "until then. Press Scan again for a complete scan." % (
                        format(st.get("walked", 0), ","), format(len(self.candidates), ","),
                        "" if len(self.candidates) == 1 else "s"))
        elif self._scanned_key is not None and folder:
            if self._scanned_key == self._scan_key():
                text = "Scanned with these settings. " + text.replace("Will look", "Looked") \
                    .replace("and open only", "and opened only").replace(
                        "and list those", "and listed those").replace("are not opened",
                                                                       "were not opened")
            else:
                text = "The list is from a scan with other settings: press Scan again. " + \
                    text
        self.plan.configure(text=text)
        self.scan_btn.configure(text="Scan again" if self._scanned_key is not None
                                else "Scan for databases",
                                state="normal" if folder else "disabled")

    def _empty_action(self):
        if self.folder_var.get():
            self.rescan()
        else:
            self.choose_folder()

    # -- scanning ------------------------------------------------------------------------
    def choose_folder(self):
        """Pick the folder; the scan starts with Scan (conditions can be added first)."""
        d = filedialog.askdirectory(parent=self, title="Folder with SQLite databases")
        if d:
            self.folder_var.set(os.path.abspath(d))
            self._update_plan()
            self._update_status()
            try:
                self.scan_btn.focus_set()
            except tk.TclError:
                pass

    def rescan(self):
        folder = self.folder_var.get()
        if not folder:
            return
        self.stop()
        if self._thread is not None:
            self._thread.join(2)
        cancel = self._cancel = [False]
        progress = self._progress = [0]
        recursive = self.recursive_var.get()
        box = {}
        accept = self._accept()
        stats = self._stats = {}
        self._scanned_with = filematch.describe(self.condition_list(), self.mode_var.get()) \
            if accept is not None else ""

        def work():
            try:
                box["result"] = scan_folder(folder, recursive, lambda: cancel[0],
                                            lambda n: progress.__setitem__(0, n),
                                            accept=accept, stats=stats)
            except Exception as e:      # noqa: BLE001 - shown in the status line
                box["result"] = ([], 0, [str(e)])
        self._found = box
        self._scan_started_key = self._scan_key()
        self.candidates, self.ticked = [], set()
        self._live_n = 0
        self.fill()
        self._thread = threading.Thread(target=work, name="scan-folder", daemon=True)
        self._thread.start()
        self.scan_btn.configure(state="disabled", text="Scanning…")
        self.stop_btn.pack(side="left", padx=(6, 0), before=self.plan)
        self.bar.pack(side="left", padx=6, before=self.plan)
        self.bar.start(15)
        self.scanning = True
        self._update_status()
        self._poll(box, cancel)

    def _poll(self, box, cancel):
        if not self.winfo_exists() or box is not self._found:
            return
        if "result" not in box:
            st = getattr(self, "_stats", None) or {}
            walked = st.get("walked") or 0
            text = "Scanning… %s files seen, %s opened, %s database%s found" % (
                format(walked, ","), format(self._progress[0], ","),
                format(st.get("found", 0), ","), "" if st.get("found", 0) == 1 else "s")
            if st.get("skipped"):
                text += " (%s not matching, not opened)" % format(st["skipped"], ",")
            live = st.get("candidates")
            if live is not None and len(live) != self._live_n:
                # the databases found so far, listed (and ticked) as they come
                new = list(live)
                self.ticked.update(c.path for c in new[self._live_n:])
                self.candidates, self._live_n = new, len(new)
                self.fill()
            self.plan.configure(text=text)
            self.status.configure(text=text)
            self.open_btn.configure(state="disabled")
            self.after(100, lambda: self._poll(box, cancel))
            return
        self.bar.stop()
        self.bar.pack_forget()
        self.stop_btn.pack_forget()
        self.scanning = False
        self._scanned_key = getattr(self, "_scan_started_key", None)
        found, seen, problems = box["result"]
        self.candidates = found
        self.ticked = set(c.path for c in found)
        self._seen, self._problems = seen, problems
        self._stopped = cancel[0]
        self.fill()
        self._update_plan()

    def wait(self, timeout=30):
        """Wait for the scan (tests): serve Tk until it is listed."""
        import time
        deadline = time.time() + timeout
        while self.scanning and time.time() < deadline:
            self.update()
            time.sleep(0.01)
        self.update()

    def stop(self):
        self._cancel[0] = True

    # -- the list ------------------------------------------------------------------------
    def shown(self):
        """The databases listed: those the find box and the conditions let through."""
        q = self.filter_var.get().strip().lower()
        conds = self.condition_list() if hasattr(self, "conditions") else []
        mode = self.mode_var.get() if hasattr(self, "mode_var") else "all"
        return [c for c in self.candidates if (not q or q in c.rel.lower()) and
                filematch.matches(conds, mode, c.name, c.rel, c.size)]

    def sort_by(self, col):
        """Sort the list by a column (a second click reverses it)."""
        cur, desc = self._sort
        self._sort = (col, (not desc) if cur == col else False)
        self.fill()

    def _ordered(self):
        col, desc = self._sort
        keys = {"name": lambda c: c.name.lower(), "size": lambda c: c.size or 0,
                "side": lambda c: c.sidecars_text() or "", "rel": lambda c: c.rel.lower()}
        order = list(enumerate(self.candidates))
        if col in keys:
            order.sort(key=lambda ic: keys[col](ic[1]), reverse=desc)
        for c, t in self._headings.items():
            arrow = (" \u25bc" if desc else " \u25b2") if c == col else ""
            self.tree.heading(c, text=t + arrow)
        return order

    def tick_shown_only(self):
        """Tick the databases listed and untick every other one."""
        self.ticked = set(c.path for c in self.shown())
        self.fill()

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        shown = set(id(c) for c in self.shown())
        for i, c in self._ordered():
            if id(c) not in shown:
                continue
            self.tree.insert("", "end", iid=str(i), values=(
                CHECK if c.path in self.ticked else UNCHECK, c.name, fmtb(c.size),
                c.sidecars_text() or "-", c.rel))
        self.search.set_count(len(shown), len(self.candidates), "database", "databases",
                              "file")
        self._update_status()

    def _update_status(self):
        if not self.folder_var.get():
            self.empty_title.configure(text="No folder chosen yet")
            self.empty_text.configure(text="1 choose the folder, 2 add conditions if you "
                                      "look for particular files (a file name, a path, a "
                                      "size...), 3 press Scan for databases.")
            self.empty_btn.configure(text="Choose folder…")
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
            text = "Choose a folder: every file starting with the SQLite header is listed."
        elif self._found is None:
            self.empty_title.configure(text="Ready to scan")
            self.empty_text.configure(text="Add conditions above if you look for particular "
                                      "files, then press Scan for databases. Nothing is "
                                      "read until then.")
            self.empty_btn.configure(text="Scan for databases")
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
            text = "Ready to scan %s." % self.folder_var.get()
        elif self._found is not None and "result" not in self._found:
            self.empty.place_forget()
            # opening waits for the scan's end; the count is that of the databases so far
            self.open_btn.configure(text="Open %d database%s" % (
                len(self.ticked), "" if len(self.ticked) == 1 else "s"), state="disabled")
            return
        else:
            n = len(self.candidates)
            text = "%d SQLite database%s in %s file%s looked at%s; %d ticked" % (
                n, "" if n == 1 else "s", format(getattr(self, "_seen", 0), ","),
                "" if getattr(self, "_seen", 0) == 1 else "s",
                " (subfolders included)" if self.recursive_var.get() else "",
                len(self.ticked))
            if not n and not self.recursive_var.get() and self._has_subfolders():
                text += " — this folder has subfolders: tick Include subfolders to look in them"
            shown = len(self.shown())
            conds = filematch.describe(self.condition_list(), self.mode_var.get())
            if conds and shown != n:
                text += " — %d shown where %s" % (shown, conds)
            stats = getattr(self, "_stats", None) or {}
            if getattr(self, "_scanned_with", "") and stats.get("skipped"):
                # (which conditions: the line beside Scan says it)
                text += " — %s other files not opened (not matching the conditions)" % (
                    format(stats["skipped"], ","))
            if getattr(self, "_stopped", False):
                text += " — scan stopped"
            if getattr(self, "_problems", None):
                text += " — " + "; ".join(self._problems[:2])
            self.empty.place_forget()
        self.status.configure(text=text)
        # ticked databases the find box or the conditions hide are still opened: said
        hidden = len(self.ticked - set(c.path for c in self.shown())) if self.candidates \
            else 0
        self.open_btn.configure(text="Open %d database%s%s" % (
            len(self.ticked), "" if len(self.ticked) == 1 else "s",
            (" (%d not shown)" % hidden) if hidden else ""),
            state="normal" if self.ticked else "disabled")

    def _has_subfolders(self):
        try:
            with os.scandir(self.folder_var.get()) as it:
                return any(e.is_dir(follow_symlinks=False) for e in it)
        except OSError:
            return False

    def _click(self, e):
        if self.tree.identify_column(e.x) != "#1":
            return None
        iid = self.tree.identify_row(e.y)
        if iid:
            self.toggle(iid)
            return "break"
        return None

    def _toggle_selected(self):
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid):
        c = self.candidates[int(iid)]
        if c.path in self.ticked:
            self.ticked.discard(c.path)
        else:
            self.ticked.add(c.path)
        self.tree.set(iid, "on", CHECK if c.path in self.ticked else UNCHECK)
        self._update_status()

    def set_all(self, on):
        """Tick (or untick) every database the filter shows."""
        for c in self.shown():
            if on:
                self.ticked.add(c.path)
            else:
                self.ticked.discard(c.path)
        self.fill()

    def ok(self):
        self.result = [c.path for c in self.candidates if c.path in self.ticked]
        # the write guard keeps out of the folder opened and of every folder a database was
        # found in, also those not chosen (they are evidence too)
        folders = [self.folder_var.get()] + [os.path.dirname(c.path) for c in self.candidates]
        self.evidence_folders = list(OrderedDict.fromkeys(f for f in folders if f))
        self.close()

    def close(self):
        self._closing = True
        self.stop()
        if getattr(self, "_cond_after", None) is not None:
            try:
                self.after_cancel(self._cond_after)
            except tk.TclError:
                pass
            self._cond_after = None
        self.destroy()
