"""The UI of a case of several databases (case.Case).

member_tooltip   the hover card text of a database: path, size, SHA-256, how it was opened.
OpenFolderDialog lists the SQLite databases of a folder (header checked) with size, WAL /
                 journal and relative path; all ticked, Select all / none, a filter,
                 optionally the subfolders. The scan runs on a thread with progress and Stop.
(The databases are listed by navigator.CaseNavigator; which ones each feature covers is
scope.ScopePicker; the tables a search covers is dialogs.ScopeDlg.for_case.)
"""

import os
import threading
from collections import OrderedDict
import tkinter as tk
from tkinter import filedialog, ttk

from constants import C
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

    def __init__(self, parent, folder=None, recursive=False, title="Open Folder",
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

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Button(top, text="Folder…", command=self.choose_folder).pack(side="left")
        self.folder_var = tk.StringVar(value=folder or "")
        ttk.Label(top, textvariable=self.folder_var, style="M.TLabel").pack(side="left", padx=6)
        self.recursive_var = tk.BooleanVar(value=recursive)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var,
                        command=self.rescan).pack(side="right")
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8, side="bottom")
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
        self.stop_btn = ttk.Button(bar, text="Stop", style="D.TButton", command=self.stop)
        # the bar and Stop are shown only while a scan runs (nothing looks busy at idle)
        self.bar = ttk.Progressbar(bar, mode="indeterminate", length=120)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        cols = (("on", "", 30), ("name", "Name", 180), ("size", "Size", 90),
                ("side", "WAL / journal", 160), ("rel", "Relative path", 380))
        self.tree = ttk.Treeview(box, columns=[c for c, _t, _w in cols], show="headings",
                                 selectmode="extended")
        for c, t, w in cols:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c == "rel", anchor="e" if c == "size" else "w")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        # empty state: before a folder is chosen the list says what to do, not a blank table
        self.empty = ttk.Frame(box, style="Card.TFrame")
        ttk.Label(self.empty, text="No folder chosen yet", style="CardHeading.TLabel").pack(pady=(0, 4))
        ttk.Label(self.empty, text="Every file that starts with the SQLite header is listed, "
                  "whatever its name or extension.", style="CardMuted.TLabel",
                  wraplength=360, justify="center").pack()
        ttk.Button(self.empty, text="Choose a folder…", style="P.TButton",
                   command=self.choose_folder).pack(pady=(10, 0))
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w")
        self.status.pack(fill="x", padx=10, side="bottom", after=bot)
        ttk.Button(bot, text="Cancel", command=self.close).pack(side="right")
        self.open_btn = ttk.Button(bot, text="Open", style="P.TButton", command=self.ok)
        self.open_btn.pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
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

    # -- scanning ------------------------------------------------------------------------
    def choose_folder(self):
        d = filedialog.askdirectory(parent=self, title="Folder with SQLite databases")
        if d:
            self.folder_var.set(os.path.abspath(d))
            self.rescan()

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

        def work():
            try:
                box["result"] = scan_folder(folder, recursive, lambda: cancel[0],
                                            lambda n: progress.__setitem__(0, n))
            except Exception as e:      # noqa: BLE001 - shown in the status line
                box["result"] = ([], 0, [str(e)])
        self._found = box
        self._thread = threading.Thread(target=work, name="scan-folder", daemon=True)
        self._thread.start()
        self.stop_btn.pack(side="right")
        self.bar.pack(side="right", padx=6)
        self.bar.start(15)
        self.scanning = True
        self._poll(box, cancel)

    def _poll(self, box, cancel):
        if not self.winfo_exists() or box is not self._found:
            return
        if "result" not in box:
            self.status.configure(text="Scanning… %s files looked at" %
                                  format(self._progress[0], ","))
            self.after(100, lambda: self._poll(box, cancel))
            return
        self.bar.stop()
        self.bar.pack_forget()
        self.stop_btn.pack_forget()
        self.scanning = False
        found, seen, problems = box["result"]
        self.candidates = found
        self.ticked = set(c.path for c in found)
        self._seen, self._problems = seen, problems
        self._stopped = cancel[0]
        self.fill()

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
        q = self.filter_var.get().strip().lower()
        return [c for c in self.candidates if not q or q in c.rel.lower()]

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        shown = set(id(c) for c in self.shown())
        for i, c in enumerate(self.candidates):
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
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
            text = "Choose a folder: every file starting with the SQLite header is listed."
        elif self._found is not None and "result" not in self._found:
            self.empty.place_forget()
            return
        else:
            n = len(self.candidates)
            text = "%d SQLite database%s in %s file%s looked at%s; %d ticked" % (
                n, "" if n == 1 else "s", format(getattr(self, "_seen", 0), ","),
                "" if getattr(self, "_seen", 0) == 1 else "s",
                " (subfolders included)" if self.recursive_var.get() else "",
                len(self.ticked))
            if getattr(self, "_stopped", False):
                text += " — scan stopped"
            if getattr(self, "_problems", None):
                text += " — " + "; ".join(self._problems[:2])
            self.empty.place_forget()
        self.status.configure(text=text)
        self.open_btn.configure(text="Open %d database%s" % (
            len(self.ticked), "" if len(self.ticked) == 1 else "s"),
            state="normal" if self.ticked else "disabled")

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
        self.stop()
        self.destroy()
