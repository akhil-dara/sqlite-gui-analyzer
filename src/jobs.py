"""Long jobs off the Tk thread, and the one export dialog and writer every tab uses.

Job: work on a worker thread with a small progress window (what it is doing, a bar, Stop).
The worker never calls into Tk: its progress and result are polled with after(). Every job
registers with the app (App._jobs) so closing the database stops it and waits for it.

export_options(): the one 'what and how to export' dialog (scope, format, how BLOBs are
written, hidden columns). export_rows(): writes rows with engine.export on a Job: it first
waits for (or computes) the SHA-256 of the evidence, then streams the rows with progress and
Stop, writes the provenance manifest next to the file, notes the export in the activity log
and says what was written, and whether it is complete.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C, VERSION
from engine import export as ex, uiyield
from utils import plural
from widgets import place_over


class Job(object):
    """Work on a worker thread with a small progress window on the Tk thread.

    work(job) runs on the worker: it checks job.cancelled, counts its progress in job.done
    (of job.total) or says what it does in job.status, and returns the result. on_done(result,
    error, cancelled) runs on the Tk thread (polled with after(), so the worker never calls
    into Tk). release() runs on the worker when it ends (e.g. to close that thread's database
    connection). The job registers in app._jobs while it runs.

    on_poll(job), when given, runs on the Tk thread at every poll while the job runs and once
    more when it has ended (before on_done): for work that hands over results as it goes.
    show_after: build the progress window only once the job has run that many seconds (a
    quick job then shows no window at all).
    """

    def __init__(self, app, title, work, on_done, total=None, show=True, release=None,
                 unit="rows", members=None, on_poll=None, show_after=0.0):
        self.app, self.title, self.total, self.unit = app, title, total, unit
        # the databases (case members) the job reads; None: not said (removing any database
        # of the case stops it)
        self.members = list(members) if members is not None else None
        self.cancelled = False
        self.done = 0
        self.status = ""
        self.result = self.error = None
        self.finished = False
        self._work, self._on_done, self._release = work, on_done, release
        self._on_poll = on_poll
        self._win = self._label = self._bar = None
        self._started = time.time()
        self._show_at = None            # when the window is built later (show_after)
        jobs = getattr(app, "_jobs", None)
        if jobs is not None:
            jobs.append(self)
        if show and show_after > 0:
            self._show_at = self._started + show_after
        elif show:
            self._build_window()
        self.thread = threading.Thread(target=self._run, name="job", daemon=True)
        try:
            self.thread.start()
            self._poll_id = app.after(100, self._poll)
        except Exception:
            # start() (or after()) failed: do not leave a phantom job in app._jobs
            # that close_database would cancel pointlessly and that never removes itself.
            jobs = getattr(app, "_jobs", None)
            if jobs is not None:
                try:
                    jobs.remove(self)
                except ValueError:
                    pass
            raise

    def _run(self):
        try:
            self.result = self._work(self)
        except Exception as e:          # noqa: BLE001 - reported on the Tk thread
            e.__traceback__ = None      # its frames hold this thread's objects
            self.error = e
        finally:
            if self._release is not None:
                try:
                    self._release()
                except Exception:       # noqa: BLE001 - nothing to report it to
                    pass
            self.finished = True

    def cancel(self):
        self.cancelled = True

    def _build_window(self):
        w = self._win = tk.Toplevel(self.app)
        w.title(self.title)
        w.configure(bg=C["bg"])
        w.transient(self.app)
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        self._label = tk.Label(w, text="Starting…", bg=C["bg"], anchor="w", width=56,
                               justify="left", wraplength=400)
        self._label.pack(fill="x", padx=12, pady=(10, 4))
        self._bar = ttk.Progressbar(w, mode="determinate" if self.total else "indeterminate",
                                    maximum=max(1, self.total or 100), length=400)
        self._bar.pack(fill="x", padx=12, pady=4)
        if not self.total:
            self._bar.start(15)
        ttk.Button(w, text="Stop", command=self.cancel).pack(pady=(4, 10))
        place_over(w, self.app)

    def text(self):
        """What the progress window says now."""
        if self.cancelled:
            return "Stopping…"
        if self.status:
            return self.status
        if self.total:
            return "%s of %s %s" % (format(self.done, ","), format(self.total, ","), self.unit)
        if self.done:
            return "%s %s" % (format(self.done, ","), self.unit)
        return "Working…"

    def wait(self):
        """Block until the job ends and run its on_done now (scripts and tests that need the
        result at once). Nothing else runs on the Tk thread meanwhile; the work must not need
        it."""
        while not self.finished:
            if self._on_poll is not None:
                self._on_poll(self)
            uiyield.beat()              # the work waited for does not give way
            self.thread.join(0.01)
        if self._poll_id is not None:
            try:
                self.app.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        self._show_at = None
        self._poll()

    def _poll(self):
        self._poll_id = None
        # read before on_poll: work handed over after on_poll looked is taken at the next
        # poll, never left behind by the end of the job
        finished = self.finished
        if self._on_poll is not None:
            self._on_poll(self)
        if not finished:
            if self._show_at is not None and time.time() >= self._show_at:
                self._show_at = None
                if not self.cancelled:
                    self._build_window()
            if self._win is not None and self._win.winfo_exists():
                self._label.configure(text=self.text())
                if self.total:
                    self._bar.configure(value=min(self.done, self.total))
            self._poll_id = self.app.after(100, self._poll)
            return
        jobs = getattr(self.app, "_jobs", None)
        if jobs is not None and self in jobs:
            jobs.remove(self)
        if self._win is not None and self._win.winfo_exists():
            self._win.destroy()
        self._on_done(self.result, self.error, self.cancelled)


# -- the export dialog ------------------------------------------------------------------------
BLOB_CHOICES = (("hex", "as hex (lossless)"), ("base64", "as base64 (lossless, smaller)"),
                ("summary", "as a summary: type, size, SHA-256 (not the bytes)"))


class ExportDialog(tk.Toplevel):
    """What to export and how: result is {'scope', 'fmt', 'blob_mode', 'skip_hidden',
    'spreadsheet_safe'} or None.

    scopes: [(key, text)] (e.g. all rows, filtered rows, selected rows); formats: the file
    formats offered ('csv', 'json'); blobs: offer the BLOB choice; hidden: offer 'Leave out
    hidden columns'; spreadsheet_safe: offer turning off the ' before formula-like CSV text
    (on by default; without the offer CSV text is always spreadsheet-safe)."""

    def __init__(self, parent, title, scopes, formats=("csv", "json"), fmt=None, blobs=True,
                 hidden=False, note="", ok_text="Export…", spreadsheet_safe=False,
                 extra_check=None, extra_for=None):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        self.scope_var = tk.StringVar(value=scopes[0][0] if scopes else "")
        if scopes:
            ttk.Label(body, text="Export", style="B.TLabel").pack(anchor="w")
            for key, text in scopes:
                ttk.Radiobutton(body, text=text, value=key,
                                variable=self.scope_var).pack(anchor="w", padx=8)
        self.fmt_var = tk.StringVar(value=fmt or formats[0])
        if len(formats) > 1:
            ttk.Label(body, text="Format", style="B.TLabel").pack(anchor="w", pady=(8, 0))
            names = {"csv": "CSV (a manifest with the provenance is written beside it)",
                     "json": "JSON (the provenance inside, and a manifest beside it)",
                     "html": "HTML report (one file, printable)",
                     "files": "Original bytes (each BLOB as it is stored)",
                     "decoded_json": "Decoded, as JSON (plists, protobuf, JSON, archives…; "
                                     "BLOBs nothing decodes are saved as they are)",
                     "plist_xml": "Plists only, as XML property lists (.plist files; other "
                                  "BLOBs are left out and counted)"}
            for f in formats:
                ttk.Radiobutton(body, text=names.get(f, f.upper()), value=f,
                                variable=self.fmt_var).pack(anchor="w", padx=8)
        self.blob_var = tk.StringVar(value="hex")
        if blobs:
            ttk.Label(body, text="BLOB values", style="B.TLabel").pack(anchor="w", pady=(8, 0))
            for key, text in BLOB_CHOICES:
                ttk.Radiobutton(body, text=text, value=key,
                                variable=self.blob_var).pack(anchor="w", padx=8)
        self.skip_var = tk.BooleanVar(value=True)
        if hidden:
            ttk.Checkbutton(body, text="Leave out hidden columns",
                            variable=self.skip_var).pack(anchor="w", pady=(8, 0))
        self.safe_var = tk.BooleanVar(value=True)
        self.safe_check = None
        if spreadsheet_safe and "csv" in formats:
            self.safe_check = ttk.Checkbutton(
                body, text="Spreadsheet-safe CSV: a ' before text starting with = + - @ "
                           "(stated in the manifest)", variable=self.safe_var)
            self.safe_check.pack(anchor="w", pady=(8, 0))
            self.fmt_var.trace_add("write", lambda *_a: self._csv_only())
            self._csv_only()
        # an extra choice for some formats (e.g. also keep the original bytes)
        self.extra_var = tk.BooleanVar(value=False)
        self.extra_check = None
        if extra_check:
            self.extra_check = ttk.Checkbutton(body, text=extra_check, variable=self.extra_var)
            self.extra_check.pack(anchor="w", pady=(8, 0))
            if extra_for:
                def _extra_state(*_a):
                    self.extra_check.state(["!disabled"] if self.fmt_var.get() in extra_for
                                           else ["disabled"])
                self.fmt_var.trace_add("write", _extra_state)
                _extra_state()
        if note:
            ttk.Label(body, text=note, style="M.TLabel", wraplength=380,
                      justify="left").pack(anchor="w", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text=ok_text, style="P.TButton", command=self._ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        place_over(self, parent)

    def _csv_only(self):
        """The spreadsheet-safe choice applies to CSV only: shown greyed for other formats."""
        if self.safe_check is not None:
            self.safe_check.state(["!disabled"] if self.fmt_var.get() == "csv"
                                  else ["disabled"])

    def _ok(self):
        self.result = {"scope": self.scope_var.get(), "fmt": self.fmt_var.get(),
                       "blob_mode": self.blob_var.get(), "skip_hidden": bool(self.skip_var.get()),
                       "spreadsheet_safe": bool(self.safe_var.get()),
                       "extra": bool(self.extra_var.get())}
        self.destroy()




class MultiTableExportDialog(tk.Toplevel):
    """Pick tables and CSV options for a multi-table export: result is
    {'tables', 'delimiter', 'encoding', 'blob_mode', 'spreadsheet_safe'} or None."""

    DELIMITERS = [(",", "Comma (,)"), (";", "Semicolon (;)"), ("\t", "Tab"), ("|", "Pipe (|)")]
    ENCODINGS = [("utf-8-sig", "UTF-8 with BOM (opens correctly in Excel)"),
                 ("utf-8", "UTF-8 (no BOM)"),
                 ("cp1252", "Western European (Windows-1252)")]

    def __init__(self, parent, tables):
        tk.Toplevel.__init__(self, parent)
        self.title("Export tables as CSV")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.result = None
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        ttk.Label(body, text="Tables", style="B.TLabel").pack(anchor="w")
        lf = ttk.Frame(body)
        lf.pack(fill="both", expand=True, pady=(4, 0))
        self._vars = {}
        self._box = tk.Listbox(lf, selectmode="extended", height=10, exportselection=False)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._box.yview)
        self._box.configure(yscrollcommand=sb.set)
        self._box.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        for t, n in tables:
            label = "%s (%s rows)" % (t, format(n, ",") if n is not None else "\u2026")
            self._box.insert("end", label)
            self._box.selection_set("end")
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Select all", command=lambda: self._box.selection_set(0, "end")).pack(side="left")
        ttk.Button(btns, text="Select none", command=lambda: self._box.selection_clear(0, "end")).pack(side="left", padx=6)
        ttk.Label(body, text="Delimiter", style="B.TLabel").pack(anchor="w", pady=(10, 0))
        self.delim_var = tk.StringVar(value=",")
        df = ttk.Frame(body)
        df.pack(anchor="w", padx=8)
        for val, text in self.DELIMITERS:
            ttk.Radiobutton(df, text=text, value=val, variable=self.delim_var).pack(side="left", padx=(0, 10))
        ttk.Label(body, text="Encoding", style="B.TLabel").pack(anchor="w", pady=(8, 0))
        self.enc_var = tk.StringVar(value="utf-8-sig")
        for val, text in self.ENCODINGS:
            ttk.Radiobutton(body, text=text, value=val, variable=self.enc_var).pack(anchor="w", padx=8)
        ttk.Label(body, text="BLOB values", style="B.TLabel").pack(anchor="w", pady=(8, 0))
        self.blob_var = tk.StringVar(value="hex")
        for key, text in BLOB_CHOICES:
            ttk.Radiobutton(body, text=text, value=key, variable=self.blob_var).pack(anchor="w", padx=8)
        self.safe_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Spreadsheet-safe: a ' before text starting with = + - @",
                        variable=self.safe_var).pack(anchor="w", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text="Export\u2026", style="P.TButton", command=self._ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self._tables = [t for t, _n in tables]
        place_over(self, parent)

    def _ok(self):
        sel = [self._tables[i] for i in self._box.curselection()]
        if not sel:
            messagebox.showinfo("Export tables", "Tick at least one table.", parent=self)
            return
        self.result = {"tables": sel, "delimiter": self.delim_var.get(),
                       "encoding": self.enc_var.get(), "blob_mode": self.blob_var.get(),
                       "spreadsheet_safe": bool(self.safe_var.get())}
        self.destroy()


def multi_table_options(parent, tables):
    """Show the MultiTableExportDialog and wait: its result (or None)."""
    dlg = MultiTableExportDialog(parent, tables)
    try:
        dlg.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(dlg)
    return dlg.result

def export_options(parent, title, scopes, **kw):
    """Show the ExportDialog and wait: its result (or None)."""
    dlg = ExportDialog(parent, title, scopes, **kw)
    try:
        dlg.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(dlg)
    return dlg.result


def ask_path(parent, fmt, name):
    """The save dialog for an export file (None when cancelled)."""
    path = filedialog.asksaveasfilename(parent=parent, defaultextension="." + fmt,
                                        initialfile=name + "." + fmt,
                                        filetypes=[(fmt.upper(), "*." + fmt), ("All", "*.*")])
    return path or None


# -- the writer on a job ----------------------------------------------------------------------
def member_databases(members):
    """(label, EvidenceSet) of each database an export reads."""
    out = []
    for m in members:
        db = getattr(m, "db", m)
        if getattr(db, "ok", False):
            out.append((getattr(m, "path", None) or db.evidence.main, db.evidence))
    return out


def evidence_records(members, cancel=None, status=None):
    """[(label, evidence_record)] of each database of `members`, waiting for (or computing)
    the SHA-256 of every evidence file; status(text) says which file is being waited for.
    On a worker thread."""
    records = []
    for label, ev in member_databases(members):
        def prog(done, tot, label=label):
            if status is not None:
                status("Waiting for the SHA-256 of %s: %d%%" % (
                    os.path.basename(label), 100 * done // max(tot, 1)))
        records.append((label, ex.evidence_record(ev, True, cancel, prog)))
    return records


def export_protected(app):
    """The evidence-folder test of the open case (None when nothing is open)."""
    return app.case.is_protected if getattr(app, "case", None) is not None and \
        len(app.case) else None


def export_rows(app, title, path, fmt, columns, rows_fn, source, members, scope="",
                filters="", blob_mode="hex", total=None, extra=None, on_done=None,
                write=None, unit="rows", spreadsheet_safe=True,
                delimiter=",", encoding=None):
    """Write rows_fn() (called on the worker thread; any iterable of value sequences) to path
    with engine.export, on a Job: waits for (or computes) the evidence hashes first, then
    streams the rows (progress, Stop), then writes the manifest. write: another writer with
    engine.export.write_rows' arguments and result (e.g. an HTML page); it must write the
    manifest too. spreadsheet_safe: CSV text starting with = + - @ gets a ' (the manifest
    says which). Returns the Job."""
    protected = export_protected(app)
    write = write or ex.write_rows

    def work(job):
        job.status = "Reading the evidence hashes…"

        def status(text):
            job.status = text
        records = evidence_records(members, lambda: job.cancelled, status)
        if job.cancelled:
            return None
        job.status = ""
        info = ex.provenance(VERSION, records, source, scope, filters, columns,
                             blob_mode=blob_mode, extra=extra, spreadsheet_safe=spreadsheet_safe)

        def progress(n):
            job.done = n
        return write(path, fmt, columns, rows_fn(), info, blob_mode,
                     lambda: job.cancelled, progress, protected,
                     delimiter=delimiter, encoding=encoding)

    def done(result, error, cancelled):
        report_export(app, result, error, fmt, source, scope, filters, blob_mode, unit)
        if on_done is not None:
            on_done(result if error is None else None)
    return Job(app, title, work, done, total=total, unit=unit, members=members,
               release=getattr(app, "_release_worker_connection", None))


def report_export(app, result, error, fmt, source, scope="", filters="", blob_mode="hex",
                  unit="rows"):
    """The end of an export, the same for every tab: logged in the activity log, and one
    message with what was written, where, its manifest, and whether it is complete."""
    if error is not None:
        messagebox.showerror("Export", "Not exported: %s" % error, parent=app)
        return
    if result is None:
        messagebox.showinfo("Export", "Stopped before anything was written.", parent=app)
        return
    app.activity("export", what=source, path=result.path, format=fmt, rows=result.rows,
                 complete=result.complete, sha256=result.sha256, manifest=result.manifest,
                 blob_mode=blob_mode, scope=scope, filters=filters)
    text = "%s written to:\n%s\n\nManifest (provenance, SHA-256 of the file):\n%s" % (
        plural(result.rows, unit[:-1] if unit.endswith("s") else unit, unit), result.path,
        result.manifest)
    if result.complete:
        messagebox.showinfo("Export", text, parent=app)
    else:
        messagebox.showwarning(
            "Export", "The export is INCOMPLETE (stopped %s). The file is kept and marked "
                      "incomplete in its manifest%s.\n\n%s" % (
                          result.stopped, " and in its 'end'" if fmt == "json" else "",
                          text), parent=app)


def write_export_manifest(app, target, source, members, files, complete, scope="",
                          filters="", rows=None, extra=None, cancel=None):
    """The manifest of an export written outside export_rows (a BLOB folder, a report, a
    map, a Copy with related file...), on the caller's worker thread: the tool and versions,
    the evidence files of `members` with their SHA-256 (waited for), what was exported and
    the SHA-256 of each written file. target: the export file (the manifest goes beside it)
    or the folder of a BLOB export (inside it). files: [(path, size, sha256)] or paths (then
    hashed). rows: how many rows or items (default: the number of files).
    Returns (its path, None), or (None, why) when it could not be written: that is also
    logged as an Issue of each database, and the caller says it (manifest_text)."""
    try:
        records = [(label, ex.evidence_record(ev, True, cancel))
                   for label, ev in member_databases(members)]
        info = ex.provenance(VERSION, records, source, scope, filters,
                             rows=len(files) if rows is None else rows, extra=extra)
        protected = app.case.is_protected if getattr(app, "case", None) is not None and \
            len(app.case) else None
        entries = [f if isinstance(f, str) else {"path": f[0], "size": f[1], "sha256": f[2]}
                   for f in files]
        return ex.write_manifest(target, info, entries, complete, protected), None
    except Exception as e:              # noqa: BLE001 - said by the caller, logged here
        why = str(e) or e.__class__.__name__
        log_issue(members, "manifest_failed", "the manifest of an export was not written: "
                  "%s" % why, target)
        return None, why


def file_written(app, path, source, members=None, parent=None, title="Export"):
    """A small file an export just wrote on the Tk thread (a BLOB saved from the inspector, a
    decoded tree): its manifest is written on a worker (it waits for the evidence SHA-256),
    then the activity log notes it and one message says where both are. members: the
    databases it comes from (default: those open)."""
    case = getattr(app, "case", None)
    if members is None:
        members = list(case) if case is not None and len(case) else []

    def work(job):
        job.status = "Writing the manifest…"
        return write_export_manifest(app, path, source, members, [path], True)

    def done(result, error, cancelled):
        manifest, why = result if error is None else (None, str(error))
        app.activity("export", what=source, path=path, manifest=manifest, manifest_error=why)
        (messagebox.showinfo if manifest else messagebox.showwarning)(
            title, "Written to:\n%s\n\n%s" % (path, manifest_text(manifest, why)),
            parent=parent or app)
    return Job(app, title, work, done, show=False, members=members,
               release=getattr(app, "_release_worker_connection", None))


def log_issue(members, kind, detail, where="", severity="warning"):
    """Log an Issue in the Issues list of each database of `members` (case members or DBs)."""
    for m in members or ():
        db = getattr(m, "db", m)
        session = getattr(db, "session", None)
        if session is not None:
            session.issues.add(kind, detail, where, severity)


def manifest_text(manifest, error):
    """The manifest line of an export's final message: where it is, or that it is missing."""
    if manifest:
        return "Manifest (provenance, SHA-256 of every file written):\n%s" % manifest
    return "Manifest NOT written: %s (logged under Issues)" % error


def blob_export_done(app, what, folder, result, error, cancelled):
    """The end of a BLOB folder export (result: count, errors, first error, (manifest, why)):
    logged in the activity log, and one message saying what was written, the manifest (or
    that it is missing), and whether it was stopped."""
    if error is not None:
        messagebox.showerror("Export BLOBs", "Not exported: %s" % error, parent=app)
        return
    count, errors, first, (manifest, why) = result
    app.activity("export", what=what, path=folder, files=count, complete=not cancelled,
                 manifest=manifest, manifest_error=why)
    msg = "%s written to:\n%s\n\n%s" % (plural(count, "BLOB file"), folder,
                                        manifest_text(manifest, why))
    if cancelled:
        msg = "STOPPED before the end: the files written so far are kept.\n\n" + msg
    if errors:
        msg += "\n\n%s could not be written, e.g.:\n%s" % (plural(errors, "BLOB"), first)
    (messagebox.showwarning if errors or cancelled or not manifest else messagebox.showinfo)(
        "Export BLOBs", msg, parent=app)
