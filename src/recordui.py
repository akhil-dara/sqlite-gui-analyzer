"""One recovered record on screen, with everything an examiner needs to cite it: where its
bytes are (file, page, offset in the page, byte offset in the file, length, WAL frame,
overflow pages), why the tool reads it as it does, its values - every part copyable, savable
and openable (a value whole, a BLOB in the inspector, the bytes themselves in a hex view).

provenance_rows(db, rec)   [(field, value)] of the record's provenance, in words and numbers
where_text(db, rec)        one line: 'page 1318 @ 690 · byte 5,397,170 (0x525E72) of
                           data.db · 412 bytes'
record_json / record_text / record_csv(db, rec)   the record with its provenance
RecordWindow               the row-detail window of a recovered record (Values, Where found,
                           Bytes)
BytesWindow                the page image the record was read from, the record highlighted
"""

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C
from date_reading import date_reading
from engine.export import csv_cell
from engine.search import value_type
from engine.fileformat.record import InvalidText
from engine.fileformat.wal import frame_no
from engine.forensics.provenance import FILE_NAMES, JOURNAL_FILE, WAL_FILE
from engine.tags import entry_from_record
from hexview import HexView
from tokens import COLOR as K, FONT as F
from utils import vb, write_allowed
from widgets import FlowFrame, SearchBox, ToolTip, TreeFilter, fit_geometry, place_over

# what a source word means, for people
SOURCE_WORDS = {
    "freeblock": "a freeblock inside a table page (space a deleted row left)",
    "unallocated": "unallocated space of a page (between the cell pointers and the cells)",
    "freelist": "a freed page (the freelist: pages the database no longer uses)",
    "orphan": "a page nothing references (no table, index or the freelist)",
    "wal": "a WAL frame holding an older copy of the page",
    "replaced": "a main-file page whose current version is in the WAL",
    "journal": "the rollback journal's copy of the page (before the last transaction)",
    "schema": "a schema row no longer in sqlite_master",
    "live": "the live row",
}
STATE_WORDS = {
    "deleted": "deleted - the table has no row with this key now",
    "older version": "an older version - the row is live with other values",
    "inserted again": "deleted, and the same values are live under another rowid",
    "unreferenced page": "on a page nothing references: deleted, or a live row a damaged "
                         "tree hides",
    "index entry": "a deleted entry of an index (its own columns only)",
}
STATE_STYLE = {"deleted": ("danger_soft", "danger_text"),
               "older version": ("warning_soft", "warning"),
               "inserted again": ("danger_soft", "muted_text"),
               "unreferenced page": ("primary_soft", "primary"),
               "index entry": ("primary_soft", "muted_text")}


def record_state(rec):
    """'deleted', 'older version', 'inserted again', 'unreferenced page' or 'index entry'."""
    if getattr(rec, "index", None) is not None:
        return "index entry"
    flags = getattr(rec, "flags", ()) or ()
    if "unreferenced_page" in flags:
        return "unreferenced page"
    if "prior_version" in flags:
        return "older version"
    if "content_live" in flags:
        return "inserted again"
    return "deleted"


def _fx(db):
    session = getattr(db, "session", None)
    return getattr(session, "forensics", None) if session is not None else None


def file_offset(db, prov):
    """Byte offset of the record in its file, or None."""
    fx = _fx(db)
    try:
        return fx.file_offset(prov) if fx is not None else None
    except Exception:               # noqa: BLE001 - no offset then, said as such
        return None


def file_path(db, prov):
    fx = _fx(db)
    try:
        return fx.file_path(prov) if fx is not None else None
    except Exception:               # noqa: BLE001
        return None


def _hex(n):
    return "0x%X" % n


def offset_text(off):
    """'5,397,170 (0x525E72)' or 'not known'."""
    return "not known" if off is None else "%s (%s)" % (format(off, ","), _hex(off))


def _p(prov, name, default=None):
    return getattr(prov, name, default)


def where_text(db, rec, with_file=True):
    """One line saying where the record's bytes are."""
    p = rec.prov
    parts = [p.where()]
    off = file_offset(db, p)
    if off is not None:
        fp = file_path(db, p)
        parts.append("byte %s%s" % (offset_text(off), " of %s" % os.path.basename(fp)
                                    if with_file and fp else ""))
    if _p(p, "length"):
        parts.append("%d bytes" % p.length)
    return " · ".join(parts)


def provenance_rows(db, rec):
    """[(field, value)]: everything known about where the record comes from."""
    p = rec.prov
    rows = []
    state = record_state(rec)
    rows.append(("State", "%s: %s" % (state, STATE_WORDS.get(state, ""))))
    rows.append(("Table", (rec.table or "(unknown: no table's schema fits)") +
                 (" - also fits: %s" % ", ".join(c for c in rec.candidates if c != rec.table)
                  if len(rec.candidates or ()) > 1 else "")))
    if getattr(rec, "index", None) is not None:
        rows.append(("Index", rec.index))
    rows.append(("Row id", "unknown (its bytes were overwritten)" if rec.rowid is None
                 else str(rec.rowid)))
    rows.append(("Confidence", rec.confidence))
    source, file = _p(p, "source", ""), _p(p, "file", "")
    rows.append(("Read from", SOURCE_WORDS.get(source, source)))
    fp = file_path(db, p)
    rows.append(("File", "%s%s" % (FILE_NAMES.get(file, file), " - %s" % fp if fp else "")))
    if file == WAL_FILE:
        rows.append(("WAL frame", "%s (%s)%s" % (frame_no(_p(p, "frame")),
                                                 _p(p, "frame_state") or "?",
                                                 ", commit %s" % p.commit_group
                                                 if _p(p, "commit_group") is not None else "")))
    rows.append(("Page", str(_p(p, "page", "?"))))
    offset, length = _p(p, "offset"), _p(p, "length")
    if offset is not None:
        rows.append(("Offset in page", "%d (%s)" % (offset, _hex(offset))))
    rows.append(("Byte offset in file", offset_text(file_offset(db, p))))
    overflow = _p(p, "overflow", ())
    if length:
        rows.append(("Length", "%d bytes (the cell on this page%s)" % (
            length, ", overflow apart" if overflow else "")))
    if overflow:
        rows.append(("Overflow pages", ", ".join(str(n) for n in overflow)))
    for c in rec.copies:
        off = file_offset(db, c)
        rows.append(("Also found at", "%s%s" % (c.where(), " · byte %s" % offset_text(off)
                                                if off is not None else "")))
    if rec.reasons:
        rows.append(("Why", "; ".join(rec.reasons)))
    if rec.flags:
        rows.append(("Flags", ", ".join(sorted(rec.flags))))
    rid = getattr(rec, "id", None)
    if rid:
        rows.append(("Record id", rid))
    return rows


def record_json(db, rec):
    """The record as JSON (values, provenance with the file's path and byte offset)."""
    fx = _fx(db)
    if fx is not None:
        from engine.forensics.report import record_dict
        d = record_dict(fx, rec)
    else:
        d = rec.as_dict()
    d["state"] = record_state(rec)
    return json.dumps(d, indent=2, ensure_ascii=False, default=str)


def record_text(db, rec):
    """The record as text: its provenance, then 'column: value' lines (BLOBs as hex)."""
    lines = ["%s: %s" % kv for kv in provenance_rows(db, rec)]
    lines.append("")
    lines.extend("%s: %s" % (c, csv_cell(v)) for c, v in zip(rec.columns, rec.values))
    return "\n".join(lines)


def record_csv(db, rec):
    """Two CSV lines: a header and the record, its provenance in columns named _state,
    _table, _rowid, _confidence, _source, _file, _page, _offset, _file_offset, _length,
    _frame before the record's own columns."""
    import csv
    import io
    p = rec.prov
    head = ["_state", "_table", "_rowid", "_confidence", "_source", "_file", "_page",
            "_offset", "_file_offset", "_length", "_frame"] + list(rec.columns)
    row = [record_state(rec), rec.table or "", "" if rec.rowid is None else rec.rowid,
           rec.confidence, _p(p, "source"), file_path(db, p) or _p(p, "file"), _p(p, "page"),
           _p(p, "offset"), file_offset(db, p), _p(p, "length"),
           frame_no(_p(p, "frame"))] + [csv_cell(v) for v in rec.values]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    w.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def record_bytes(db, rec):
    """(page image, cell offset, length) the record was read from, or None."""
    fx = _fx(db)
    if fx is None:
        return None
    try:
        image = fx.page_image(rec.prov)
    except Exception:               # noqa: BLE001 - a record that knows no page
        image = None
    if image is None:
        return None
    return image, _p(rec.prov, "offset"), _p(rec.prov, "length")


def _copy(widget, text):
    widget.clipboard_clear()
    widget.clipboard_append(text)


def _save_text(parent, text, initialfile, kind, binary=False):
    """Save text (or bytes) to a file the user picks (never inside the evidence folder)."""
    path = filedialog.asksaveasfilename(parent=parent, initialfile=initialfile,
                                        defaultextension=os.path.splitext(initialfile)[1],
                                        filetypes=[(kind, "*" + os.path.splitext(
                                            initialfile)[1]), ("All files", "*.*")])
    if not path:
        return None
    if not write_allowed(path):
        messagebox.showwarning("Not written", "That folder holds evidence: nothing is "
                                              "written there. Choose another folder.",
                               parent=parent)
        return None
    if binary:
        with open(path, "wb") as f:
            f.write(text)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return path


def info_block(parent, text, bg, fg, font):
    """A block of facts the user can select and copy (Ctrl+C, or right-click \u203a Copy):
    a read-only text drawn like a label, as tall as its lines."""
    lines = text.count("\n") + 1
    t = tk.Text(parent, height=lines, wrap="word", bg=bg, fg=fg, font=font, relief="flat",
                bd=0, highlightthickness=0, padx=0, pady=0, cursor="xterm",
                insertbackground=bg)
    t.insert("1.0", text)
    t.configure(state="disabled")
    t.tag_configure("sel", background=K["selection"], foreground=K["text"])

    def wrap(e):
        try:
            w = e.width
            if w > 40:
                t.configure(height=max(lines, _wrapped_lines(t, text, w)))
        except tk.TclError:
            pass
    t.bind("<Configure>", wrap)

    def copy_all():
        _copy(t, text)

    def copy_sel():
        try:
            _copy(t, t.get("sel.first", "sel.last"))
        except tk.TclError:
            copy_all()
    menu = tk.Menu(t, tearoff=0)
    menu.add_command(label="Copy", command=copy_sel)
    menu.add_command(label="Copy all", command=copy_all)
    menu.add_command(label="Select all", command=lambda: t.tag_add("sel", "1.0", "end-1c"))
    t.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
    t.bind("<Control-c>", lambda e: (copy_sel(), "break")[1])
    t.bind("<Control-a>", lambda e: (t.tag_add("sel", "1.0", "end-1c"), "break")[1])
    t.text = text
    t.copy_all = copy_all
    t.copy_selection = copy_sel
    return t


def _wrapped_lines(t, text, width):
    """Lines the text takes at this width (measured with the widget's font)."""
    import tkinter.font as tkfont
    try:
        font = tkfont.Font(font=t.cget("font"))
    except tk.TclError:
        return text.count("\n") + 1
    n = 0
    for line in text.split("\n"):
        n += max(1, -(-font.measure(line) // max(1, width - 8)))
    return n


class BytesWindow(tk.Toplevel):
    """The page image a record was read from, the record's bytes highlighted."""

    def __init__(self, parent, app, db, rec):
        tk.Toplevel.__init__(self, parent)
        self.app, self.db, self.rec = app, db, rec
        p = rec.prov
        self.title("Bytes of the recovered record — %s, %s" % (rec.table or "unknown table",
                                                              p.where()))
        fit_geometry(self, 900, 600)
        place_over(self, parent)
        self.configure(bg=C["bg"])
        got = record_bytes(db, rec)
        off = file_offset(db, p)
        fp = file_path(db, p)
        head = tk.Frame(self, bg=K["primary_soft"], padx=10, pady=6)
        head.pack(fill="x")
        if got is None:
            text = "The page could not be read."
        else:
            image, start, length = got
            text = ("%s (%d bytes) of %s\nrecord at page offset %s = byte %s of the file · "
                    "%s bytes%s" % (
                        p.where().split(" @")[0], len(image), fp or FILE_NAMES.get(p.file, p.file),
                        "?" if start is None else "%d (%s)" % (start, _hex(start)),
                        offset_text(off), length or "?",
                        " · the highlighted bytes are the record" if start is not None else ""))
        self.head_text = info_block(head, text, K["primary_soft"], K["primary"], F["small"])
        self.head_text.pack(fill="x")
        self.hex = HexView(self)
        self.hex.pack(fill="both", expand=True, padx=8, pady=6)
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        if got is not None:
            image, start, length = got
            self.hex.set_data(image)
            if start is not None and length:
                self.hex.highlight(start, length)
                self.hex.goto(start)
            bar.add(ttk.Button(bar, text="Copy record bytes (hex)", command=lambda: _copy(
                self, image[start:start + length].hex() if start is not None else "")))
            bar.add(ttk.Button(bar, text="Copy page bytes (hex)",
                               command=lambda: _copy(self, image.hex())), gap=4)
            bar.add(ttk.Button(bar, text="Save page bytes…", command=lambda: _save_text(
                self, image, "page_%s.bin" % p.page, "Binary", binary=True)), gap=4)
        bar.add(ttk.Button(bar, text="Close", command=self.destroy), gap=12)
        self.bind("<Escape>", lambda e: self.destroy())


class RecordWindow(tk.Toplevel):
    """One recovered record: Values, Where found (its provenance), Bytes; every part
    copyable (right-click, Ctrl+C, Copy ▾) and savable (Save ▾)."""

    def __init__(self, parent, record, app):
        tk.Toplevel.__init__(self, parent)
        self.record, self.app = record, app
        self.db = getattr(app, "db", None)
        r, p = record, record.prov
        state = record_state(r)
        self.title("Row detail — recovered record, %s%s" % (
            r.table or "unknown table", "" if r.rowid is None else ", rowid %s" % r.rowid))
        fit_geometry(self, 860, 600)
        place_over(self, parent)
        self.configure(bg=C["bg"])
        bg_key, fg_key = STATE_STYLE.get(state, ("primary_soft", "primary"))
        bg, fg = K[bg_key], K[fg_key]
        head = tk.Frame(self, bg=bg, padx=12, pady=8)
        head.pack(fill="x")
        what = r.table or "(unknown table)"
        if getattr(r, "index", None) is not None:
            what = "index %s of %s" % (r.index, r.table)
        info_block(head, "RECOVERED RECORD  ·  %s  ·  %s" % (state, what), bg, fg,
                   F["heading"]).pack(fill="x")
        sub = "%s confidence  ·  rowid %s  ·  %s" % (
            r.confidence, "unknown" if r.rowid is None else r.rowid, where_text(self.db, r))
        info_block(head, sub, bg, fg, F["body"]).pack(fill="x")
        why = "; ".join(r.reasons)
        if why:
            lbl = tk.Label(head, text=why, bg=bg, fg=fg, font=F["small"], anchor="w",
                           justify="left")
            lbl.pack(fill="x", pady=(2, 0))
            lbl.bind("<Configure>", lambda e: lbl.configure(wraplength=max(200, e.width - 8)))

        nb = self.nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        # -- Values
        vals = ttk.Frame(nb)
        nb.add(vals, text="  Values (%d)  " % len(r.columns))
        fbar = ttk.Frame(vals)
        fbar.pack(fill="x", pady=(6, 2))
        self.find = SearchBox(fbar, placeholder="Find a column or value…", delay=100,
                              primary=True, width=30)
        self.find.pack(side="left", fill="x", expand=True)
        box = ttk.Frame(vals)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("col", "value", "type", "date"), show="headings")
        for c, text, w in (("col", "Column", 170), ("value", "Value", 380), ("type", "Type", 60),
                           ("date", "Date (UTC)", 200)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, stretch=(c == "value"), minwidth=40)
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for i, (c, v) in enumerate(zip(r.columns, r.values)):
            self.tree.insert("", "end", iid=str(i),
                             values=(c, vb(v), value_type(v),
                                     date_reading(self.db, r.table, c, v)))
        self.filter = TreeFilter(self.tree, self.find, "column", "columns")
        self.tree.bind("<Double-1>", self._open_value)
        self.tree.bind("<Return>", self._open_value)
        self.tree.bind("<Button-3>", self._value_menu)
        self.tree.bind("<Control-c>", lambda e: self._copy_line("value"))
        ttk.Label(vals, text="Double-click or Enter: the value whole (JSON pretty-printed, a "
                             "BLOB in the inspector) · right-click or Ctrl+C: copy",
                  style="M.TLabel").pack(anchor="w", pady=(2, 4))
        # -- Where found
        prov = ttk.Frame(nb)
        nb.add(prov, text="  Where found  ")
        pbox = ttk.Frame(prov)
        pbox.pack(fill="both", expand=True, pady=(6, 0))
        self.ptree = ttk.Treeview(pbox, columns=("field", "value"), show="headings")
        self.ptree.heading("field", text="Where found")
        self.ptree.heading("value", text="")
        self.ptree.column("field", width=160, stretch=False, minwidth=100)
        self.ptree.column("value", width=620, stretch=True, minwidth=100)
        psb = ttk.Scrollbar(pbox, orient="vertical", command=self.ptree.yview)
        self.ptree.configure(yscrollcommand=psb.set)
        psb.pack(side="right", fill="y")
        self.ptree.pack(fill="both", expand=True)
        self.prov_rows = provenance_rows(self.db, r)
        for i, (k, v) in enumerate(self.prov_rows):
            self.ptree.insert("", "end", iid=str(i), values=(k, v))
        self.ptree.bind("<Button-3>", self._prov_menu)
        self.ptree.bind("<Control-c>", lambda e: self._copy_prov("value"))
        pbar = FlowFrame(prov)
        pbar.pack(fill="x", pady=4)
        pbar.add(ttk.Button(pbar, text="Copy all (text)", style="Small.TButton",
                            command=lambda: _copy(self, "\n".join(
                                "%s: %s" % kv for kv in self.prov_rows))))
        pbar.add(ttk.Button(pbar, text="Copy where (one line)", style="Small.TButton",
                            command=lambda: _copy(self, where_text(self.db, r))), gap=4)
        pbar.add(ttk.Button(pbar, text="Open the bytes…", style="Small.TButton",
                            command=self.open_bytes), gap=4)
        # -- Bytes (filled when first shown)
        self.bytes_tab = ttk.Frame(nb)
        nb.add(self.bytes_tab, text="  Bytes  ")
        self._hex = None
        nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        btns = FlowFrame(self)
        btns.pack(fill="x", padx=8, pady=8)
        copy_btn = btns.add(ttk.Button(btns, text="Copy ▾"))
        copy_btn.configure(command=lambda: self._post(copy_btn, self._copy_menu()))
        ToolTip(copy_btn, "The whole record: as JSON (values and provenance), CSV, text, or "
                          "its provenance alone")
        save_btn = btns.add(ttk.Button(btns, text="Save ▾"), gap=4)
        save_btn.configure(command=lambda: self._post(save_btn, self._save_menu()))
        if r.table and r.rowid is not None:
            btns.add(ttk.Button(btns, text="Row history", command=self._history), gap=4)
        tag_btn = btns.add(ttk.Button(btns, text="Tag ▾"), gap=4)
        tag_btn.configure(command=lambda: self._tag_popup(tag_btn))
        btns.add(ttk.Button(btns, text="Close", command=self.destroy), gap=16)
        self.bind("<Escape>", lambda e: self.destroy())

    # -- values -------------------------------------------------------------------------------
    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        i = int(sel[0])
        return self.record.columns[i], self.record.values[i]

    def _open_value(self, _e=None):
        col, v = self._selected()
        if col is None:
            return
        if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText):
            self.app.open_blob(bytes(v), col, "%s.%s (recovered, %s)" % (
                self.record.table or "?", col, self.record.prov.where()))
            return
        from value_viewer import ValueViewer
        ValueViewer(self, v, "%s — recovered record, %s" % (col, self.record.prov.where()))

    def _copy_line(self, what):
        col, v = self._selected()
        if col is None:
            return "break"
        value = csv_cell(v)
        _copy(self, {"name": col, "value": value}.get(what, "%s: %s" % (col, value)))
        return "break"

    def _value_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        col, v = self._selected()
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy value", accelerator="Ctrl+C",
                      command=lambda: self._copy_line("value"))
        m.add_command(label="Copy column name", command=lambda: self._copy_line("name"))
        m.add_command(label="Copy name: value", command=lambda: self._copy_line("both"))
        m.add_separator()
        blob = isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText)
        m.add_command(label="Inspect BLOB…" if blob else "View value…", accelerator="Enter",
                      command=self._open_value)
        self._post_at(m, e.x_root, e.y_root)

    # -- provenance -----------------------------------------------------------------------------
    def _copy_prov(self, what):
        sel = self.ptree.selection()
        if not sel:
            return "break"
        k, v = self.prov_rows[int(sel[0])]
        _copy(self, {"value": v, "name": k}.get(what, "%s: %s" % (k, v)))
        return "break"

    def _prov_menu(self, e):
        iid = self.ptree.identify_row(e.y)
        if not iid:
            return
        self.ptree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy value", accelerator="Ctrl+C",
                      command=lambda: self._copy_prov("value"))
        m.add_command(label="Copy field: value", command=lambda: self._copy_prov("both"))
        m.add_command(label="Copy all", command=lambda: _copy(
            self, "\n".join("%s: %s" % kv for kv in self.prov_rows)))
        self._post_at(m, e.x_root, e.y_root)

    def _tab_changed(self, _e=None):
        if self.nb.select() == str(self.bytes_tab) and self._hex is None:
            self._fill_bytes()

    def _fill_bytes(self):
        got = record_bytes(self.db, self.record)
        p = self.record.prov
        if got is None:
            ttk.Label(self.bytes_tab, text="The page could not be read.",
                      style="M.TLabel").pack(anchor="w", padx=4, pady=8)
            self._hex = False
            return
        image, start, length = got
        off = file_offset(self.db, p)
        ttk.Label(self.bytes_tab, text="%s (%d bytes) · the record at page offset %s = byte "
                                       "%s of %s · %s bytes highlighted" % (
                                           p.where().split(" @")[0], len(image),
                                           "?" if start is None else start, offset_text(off),
                                           os.path.basename(file_path(self.db, p) or
                                                            FILE_NAMES.get(p.file, p.file)),
                                           length or "?"),
                  style="M.TLabel").pack(anchor="w", padx=4, pady=(6, 2))
        self._hex = HexView(self.bytes_tab)
        self._hex.pack(fill="both", expand=True, padx=2, pady=(0, 4))
        self._hex.set_data(image)
        if start is not None and length:
            self._hex.highlight(start, length)
            self._hex.goto(start)
        bar = FlowFrame(self.bytes_tab)
        bar.pack(fill="x", pady=(0, 4))
        bar.add(ttk.Button(bar, text="Copy record bytes (hex)", style="Small.TButton",
                           command=lambda: _copy(self, image[start:start + length].hex()
                                                 if start is not None and length else "")))
        bar.add(ttk.Button(bar, text="Copy page bytes (hex)", style="Small.TButton",
                           command=lambda: _copy(self, image.hex())), gap=4)
        bar.add(ttk.Button(bar, text="Open in its own window", style="Small.TButton",
                           command=self.open_bytes), gap=4)

    def open_bytes(self):
        BytesWindow(self, self.app, self.db, self.record)

    # -- menus ---------------------------------------------------------------------------------
    def _copy_menu(self):
        m = tk.Menu(self, tearoff=0)
        db, r = self.db, self.record
        m.add_command(label="JSON (values and provenance)",
                      command=lambda: _copy(self, record_json(db, r)))
        m.add_command(label="CSV (one header line, one record line)",
                      command=lambda: _copy(self, record_csv(db, r)))
        m.add_command(label="Text (provenance, then column: value)",
                      command=lambda: _copy(self, record_text(db, r)))
        m.add_command(label="Values as TSV (column, value per line)",
                      command=lambda: _copy(self, "\n".join(
                          "%s\t%s" % (c, csv_cell(v)) for c, v in zip(r.columns, r.values))))
        m.add_separator()
        m.add_command(label="Provenance only", command=lambda: _copy(
            self, "\n".join("%s: %s" % kv for kv in provenance_rows(db, r))))
        m.add_command(label="Where (one line)", command=lambda: _copy(self, where_text(db, r)))
        return m

    def _save_menu(self):
        m = tk.Menu(self, tearoff=0)
        db, r = self.db, self.record
        stem = "recovered_%s_%s" % (r.table or "record", getattr(r, "id", "") or "record")
        m.add_command(label="JSON file…", command=lambda: _save_text(
            self, record_json(db, r), stem + ".json", "JSON"))
        m.add_command(label="CSV file…", command=lambda: _save_text(
            self, record_csv(db, r), stem + ".csv", "CSV"))
        m.add_command(label="Text file…", command=lambda: _save_text(
            self, record_text(db, r), stem + ".txt", "Text"))

        def save_bytes():
            got = record_bytes(db, r)
            if got is None or got[1] is None or not got[2]:
                messagebox.showinfo("Bytes", "The record's bytes could not be read.", parent=self)
                return
            image, start, length = got
            _save_text(self, image[start:start + length], stem + ".bin", "Binary", binary=True)
        m.add_command(label="Record bytes (.bin)…", command=save_bytes)
        return m

    def _post(self, button, menu):
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _post_at(self, menu, x, y):
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _history(self):
        self.app.show_row_history(self.record.table, self.record.rowid)
        self.destroy()

    def _tag_popup(self, button):
        menu = tk.Menu(self, tearoff=0)
        self.app.tag_menu(menu, lambda: [entry_from_record(self.record)], label="Tag this record")
        self._post(button, menu)

    def _copy_json(self):
        _copy(self, record_json(self.db, self.record))
