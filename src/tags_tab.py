"""The Tagged tab: every tagged row of the open database with its tags and note, filtered by
tag, by text and by column filters. The list is a virtual DataGrid, so any number of tagged
rows scrolls smoothly; each row shows its first tag's colour. Double-click opens the row (the
row window, the WAL record window or the recovered-record window). Buttons edit notes, remove
tags, manage the tag list (rename, recolour, delete, reorder), export (HTML report, CSV folder,
JSON) and save or load the tag file. The rows come from app.tags (tagging.Tagging); this
module only shows them.
"""

import os
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from browse_sources import ListSource
from combobox import SearchableCombobox
from constants import C
from tokens import COLOR as K, FONT as F
from database import RID
from engine.tag_export import provenance_text
from engine.tags import TagError, tint
from grid import DataGrid
from utils import safe_filename, write_allowed
from widgets import ElideLabel, FlowFrame, SearchBox, ToolTip, TreeFilter, fit_geometry

PREVIEW_VALUES = 4


class EntrySource(ListSource):
    """The tagged rows as a grid source whose rows can be replaced in place (the grid keeps
    its sort, filters and position)."""

    def replace(self, rows):
        self._all = list(rows)
        self._rebuild()


def _preview(entry):
    """First values of the row as short text, from the stored (JSON-safe) values: BLOBs are
    described by their size without decoding them."""
    parts = []
    for c, v in list(zip(entry.columns, entry.values))[:PREVIEW_VALUES]:
        if v is None:
            s = "NULL"
        elif isinstance(v, dict):
            if "$invalid_text_b64" in v:
                s = "\u26a0 invalid text"
            elif "$real" in v:
                s = v["$real"]
            elif "size" in v:
                s = "[BLOB %s bytes]" % format(v.get("size") or 0, ",")
            else:
                s = "?"
        else:
            s = str(v).replace("\r", " ").replace("\n", " ")
            if len(s) > 40:
                s = s[:40] + "\u2026"
        parts.append("%s=%s" % (c, s))
    return "  ".join(parts)


class TagsTab(ttk.Frame):
    """The Tagged tab (app.py adds it to the notebook)."""

    COLUMNS = (("tags", "Tags", 170), ("table", "Table", 160), ("row", "Row", 90),
               ("source", "Source", 210), ("note", "Note", 220),
               ("tagged", "Tagged at (UTC)", 150), ("preview", "Preview", 420))
    DATABASE_COLUMN = ("database", "Database", 150)     # listed first when 2+ databases are open

    def __init__(self, master, app):
        ttk.Frame.__init__(self, master)
        self.app, self.tags = app, app.tags
        app.tags.tab = self
        self._multi = False
        self._shown = []                # entries of the chosen tag; grid rows point into it
        self._labels = {}
        self._filter_after = None
        self._colors = {}

        top = FlowFrame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        top.add(ttk.Label(top, text="Tag:"))
        self.tag_var = tk.StringVar(value="All")
        self.tag_combo = top.add(SearchableCombobox(top, textvariable=self.tag_var,
                                              state="readonly", width=26))
        self.tag_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.search = top.add(SearchBox(
            top, placeholder="Find tagged rows (words, all must match)…", delay=0,
            find_button=False, primary=True, width=26, on_change=lambda t: self._debounce(),
            on_next=lambda forward: self.grid.focus_set(),
            tooltip="Keep rows whose tags, table, row, source, note or values contain all "
                    "these words (any case). Each column also has its own filter under its "
                    "header."), stretch=True, gap=12)
        self.filter_var = self.search.var
        self.filter_entry = self.search.entry
        self.buttons = {}
        for name, text, cmd in (("note", "Edit note", self.edit_note),
                                ("remove", "Remove tag", self._remove_menu),
                                ("manage", "Manage tags\u2026", self.manage_tags),
                                ("export", "Export\u2026", self.export_dialog),
                                ("save", "Save tags as\u2026", self.save_tags_as),
                                ("load", "Load tags\u2026", self.load_tags)):
            self.buttons[name] = top.add(ttk.Button(top, text=text, command=cmd), gap=4)
        ToolTip(self.buttons["export"], "HTML report, CSV folder or JSON of the tagged rows,\n"
                                        "with the evidence hashes; never into the evidence folder")
        ToolTip(self.buttons["load"], "Merge a tag file or a JSON export into these tags")

        self.warn_lbl = tk.Label(self, text="", anchor="w", justify="left", bg=K["warning_soft"],
                                 fg=C["orange"], font=F["small"], padx=8, pady=2,
                                 wraplength=1400)
        # the tag file's full path is long: shortened in the middle, whole in its tooltip
        self.status = ElideLabel(self, text="", style="M.TLabel", anchor="w")
        self.status.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        box = tk.Frame(self, bg=C["bg"])
        box.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        self._box = box
        self.grid = DataGrid(box, frozen=1, on_open_row=self._open_row,
                             row_style=self._row_style, on_context_menu=self._context_menu,
                             on_filter=lambda _e, _g: self._filters_changed())
        self.grid.pack(fill="both", expand=True)
        self.grid.use_search_box(self.search)       # the tab's own search field, not two
        self.grid.bind_key("<Delete>", self._remove_menu)
        self._set_source()
        self.refresh()

    def columns(self):
        """The list's columns: the database first when several databases are open."""
        return ((self.DATABASE_COLUMN,) if self._multi else ()) + self.COLUMNS

    def _set_source(self):
        cols = self.columns()
        self._source = EntrySource([RID] + [t for _c, t, _w in cols], [])
        self.grid.set_source(self._source)
        for i, (_c, _t, w) in enumerate(cols, 1):
            self.grid.set_column_width(i, w)

    # -- the tags of every open database (the Tagging API; a single store without it) ---------
    def _is_multi(self):
        fn = getattr(self.tags, "multi", None)
        return bool(fn()) if fn is not None else False

    def _entries(self, tag=None):
        fn = getattr(self.tags, "all_entries", None)
        if fn is not None:
            return fn(tag)
        store = self.tags.store
        return store.entries(tag) if store is not None else []

    def _total(self):
        fn = getattr(self.tags, "total", None)
        if fn is not None:
            return fn()
        return len(self.tags.store) if self.tags.store is not None else 0

    def _counts(self):
        fn = getattr(self.tags, "counts", None)
        return fn() if fn is not None else self.tags.store.counts()

    def _stored(self, case_key):
        """The stored entry of a listed row's case key (None when it is no longer tagged)."""
        fn = getattr(self.tags, "entry_by_case_key", None)
        if fn is not None:
            return fn(case_key)
        store = self.tags.store
        return store.get(case_key.partition("\x1f")[2]) if store is not None else None

    def _store_of(self, entry):
        fn = getattr(self.tags, "store_for", None)
        return fn(entry) if fn is not None else self.tags.store

    # -- content ---------------------------------------------------------------------------------
    def selected_tag(self):
        """The tag chosen in the Tag box (None: all tags)."""
        return self._labels.get(self.tag_var.get())

    def filtered_entries(self):
        """Entries of the chosen tag the list shows now (the text filter and the column
        filters applied), in the list's order."""
        if self.tags.store is None:
            return []
        out = []
        for values in self._source.iter_rows():
            e = self._entry_of(values)
            if e is not None:
                out.append(e)
        return out

    def _entry_of(self, values):
        try:
            return self._shown[int(values[0]) - 1]
        except (IndexError, TypeError, ValueError):
            return None

    def _values(self, e):
        prov = provenance_text(e)
        note = e.note.replace("\r", " ").replace("\n", " \u21b5 ")
        rowid = e.rowid
        if isinstance(rowid, str) and rowid.lstrip("-").isdigit() and len(rowid) < 20:
            rowid = int(rowid)          # sorts as a number
        out = ("\u25cf " + ", ".join(e.tags), e.table, rowid,
               e.source + (" \u2014 " + prov if prov else ""),
               note if len(note) <= 200 else note[:200] + "\u2026",
               e.tagged_at.replace("T", " ").rstrip("Z")[:19], _preview(e))
        if self._multi:
            fn = getattr(self.tags, "database_name", None)
            out = ((fn(e) if fn is not None else "") or
                   (e.database or {}).get("name") or "?",) + out
        return out

    def refresh(self):
        """Rebuild the tag box and the list (keeps the chosen tag and the selected rows)."""
        store = self.tags.store
        chosen = self.selected_tag()
        multi = self._is_multi()
        if multi != self._multi:
            self._multi = multi
            self._set_source()
        if store is None:
            labels = ["All"]
            self._labels = {"All": None}
        else:
            counts = self._counts()
            labels = ["All (%s)" % format(self._total(), ",")]
            self._labels = {labels[0]: None}
            for name, n in counts.items():
                lab = "%s (%s)" % (name, format(n, ","))
                labels.append(lab)
                self._labels[lab] = name
        self.tag_combo.configure(values=labels)
        self.tag_var.set(next((lab for lab, t in self._labels.items() if t == chosen and
                               (t is not None or lab == labels[0])), labels[0]))
        selected = self.tags_keys_selected()
        self._colors = {}
        if store is not None:
            for d in store.defs:
                color = store.color_of(d.name)
                self._colors[d.name] = (tint(color), color)
        self._shown = self._entries(self.selected_tag()) if store is not None else []
        self._source.replace(([i + 1] + list(self._values(e)), ())
                             for i, e in enumerate(self._shown))
        self.grid.refresh()
        self.grid.row_count_changed()
        self._reselect(selected)
        self._matched = self._source.row_count()
        self.show_status()
        n = self._total() if store is not None else 0
        try:
            self.app._nb.tab(self, text="  Tagged (%s)  " % format(n, ",") if n else "  Tagged  ")
        except tk.TclError:
            pass                        # not in the notebook (tests)
        if store is None:
            self.show_warning(None)

    def _selected_values(self):
        sel = self.grid.selected_rows()
        if sel is None or self.grid.row_count() <= 0:
            return []
        return [values for values, _flags in self.grid.fetch_rows(sel[0], sel[1])]

    def tags_keys_selected(self):
        """Keys of the selected rows (database identity + key: rows of two databases can
        share a key)."""
        out = []
        for values in self._selected_values():
            e = self._entry_of(values)
            if e is not None:
                out.append(e.case_key)
        return out

    def selected_entries(self):
        if self.tags.store is None:
            return []
        out = []
        for k in self.tags_keys_selected():
            e = self._stored(k)
            if e is not None:
                out.append(e)
        return out

    def select_rows(self, lo, hi=None):
        """Select the listed rows lo..hi (as a click and a Shift+click would)."""
        self.grid.set_current_cell(lo, None)
        if hi is not None and hi != lo:
            self.grid.set_current_cell(hi, None, extend=True)

    def _reselect(self, keys):
        """After the rows changed, select again the rows of these keys (the span from the
        first to the last of them that are still listed)."""
        if not keys:
            return
        want = set(keys)
        rows = [i for i, values in enumerate(self._source.iter_rows())
                if (self._entry_of(values) is not None and
                    self._entry_of(values).case_key in want)]
        if rows:
            self.select_rows(rows[0], rows[-1])

    def _row_style(self, _row, values, _flags):
        e = self._entry_of(values)
        if e is None or not e.tags:
            return None, None
        return self._colors.get(e.tags[0], (None, None))

    def show_status(self):
        store = self.tags.store
        if store is None:
            text = "Open a database to tag its rows (right-click a row in Browse or a search " \
                   "result, or press Ctrl+T)."
        else:
            total = self._total()
            text = "%s tagged row%s" % (format(total, ","), "" if total == 1 else "s")
            matched = getattr(self, "_matched", 0)
            if matched != total:
                text += "  |  %s listed" % format(matched, ",")
            if self._multi:
                stores = list(getattr(self.tags, "stores", {}).values())
                text += "  |  %d databases, each saved in its own tag file in the app-data " \
                        "folder, never next to the evidence (hover for where)" % len(stores)
                where = "\n".join(s.path for s in stores if getattr(s, "path", None))
            else:
                text += "  |  saved in the app-data folder, never next to the evidence " \
                        "(hover for the file)"
                where = store.path
        if self.tags.save_error:
            text += "  |  " + self.tags.save_error
        self.status.configure(text=text)
        tip = getattr(self.status, "_tip", None)
        if tip is not None and store is not None:
            tip.text = "%s\n\n%s" % (text, where)    # the tag file(s), in full

    def show_warning(self, text):
        """Show a warning above the list (None hides it)."""
        if text:
            self.warn_lbl.configure(text="\u26a0 " + text)
            if not self.warn_lbl.winfo_manager():
                self.warn_lbl.pack(fill="x", padx=8, pady=(0, 2), before=self._box)
        elif self.warn_lbl.winfo_manager():
            self.warn_lbl.pack_forget()

    def _debounce(self):
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
        self._filter_after = self.after(250, self._apply_filter)

    def _apply_filter(self):
        if self._filter_after is not None:
            try:
                self.after_cancel(self._filter_after)
            except tk.TclError:
                pass
        self._filter_after = None
        self.grid.set_global_filter(self.filter_var.get(), apply=True)
        self._filters_changed()

    def _filters_changed(self):
        self._matched = self._source.row_count()
        self.search.set_count(self._matched, self._total(), "row", "rows", "tagged row")
        self.show_status()

    # -- actions -----------------------------------------------------------------------------------
    def open_selected(self):
        sel = self.selected_entries()
        if sel:
            self.tags.open_entry(sel[0])
        return "break"

    def _open_row(self, _row, values):
        e = self._entry_of(values)
        cur = self._stored(e.case_key) if e is not None and self.tags.store is not None else None
        if cur is not None:
            self.tags.open_entry(cur)

    def edit_note(self):
        sel = self.selected_entries()
        if sel:
            self.tags.edit_note(sel)

    def remove_tag(self, tag=None, entries=None):
        """Take `tag` (every tag when None) off the entries (default: the selected ones)."""
        store = self.tags.store
        entries = self.selected_entries() if entries is None else entries
        if store is None or not entries:
            return 0
        groups = {}
        for e in entries:
            s = self._store_of(e)
            groups.setdefault(id(s), (s, []))[1].append(e.key)
        n = sum(s.remove(keys, tag) for s, keys in groups.values())
        self.tags.changed()
        log = getattr(self.tags, "log", None)
        if log is not None:
            log("untag" if tag else "remove all tags", entries, tag, n)
        return n

    def _remove_menu(self):
        sel = self.selected_entries()
        if not sel:
            return
        m = tk.Menu(self, tearoff=0)
        chosen = self.selected_tag()
        names = []
        for e in sel:
            for t in e.tags:
                if t not in names:
                    names.append(t)
        for t in names:
            m.add_command(label="Remove '%s'%s" % (t, " (the tag shown)" if t == chosen else ""),
                          image=self.tags.swatch(self.tags.store.color_of(t)), compound="left",
                          command=lambda t=t: self.remove_tag(t, sel))
        m.add_separator()
        m.add_command(label="Remove all tags of %d row%s" % (len(sel), "" if len(sel) == 1
                                                             else "s"),
                      command=lambda: self.remove_tag(None, sel))
        b = self.buttons["remove"]
        try:
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            m.grab_release()

    def _context_menu(self, m, _row, _col):
        """Right-click on the list: the grid's own entries, then the tag actions for the
        selected rows."""
        sel = self.selected_entries()
        if not sel:
            return
        m.add_separator()
        self.tags.tag_menu(m, lambda: [e.copy() for e in sel])
        m.add_command(label="Edit note\u2026", command=self.edit_note)
        m.add_command(label="Remove tag\u2026", command=self._remove_menu)
        m.add_command(label="Copy key", command=lambda: (self.clipboard_clear(),
                                                         self.clipboard_append(
                                                             "\n".join(x.key for x in sel))))

    def manage_tags(self):
        if self.tags.store is not None:
            return ManageTagsDialog(self)
        return None

    def save_tags_as(self, path=None):
        """Write the tag file to a place the user chooses (never the evidence folder)."""
        store = self.tags.store
        if store is None:
            return None
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self, title="Save tags as", defaultextension=".json",
                initialfile="%s-tags.json" % ("case" if self._multi else safe_filename(
                    os.path.basename(store.db_path), 60)),
                filetypes=[("Tag file (JSON)", "*.json"), ("All files", "*.*")])
        if not write_allowed(path):
            return None
        try:
            # a case: the rows of every database in one file, each naming its database
            store.save_as(path, self._entries() if self._multi else None)
        except (OSError, TagError) as e:
            messagebox.showerror("Save tags", str(e), parent=self)
            return None
        return path

    def load_tags(self, path=None, quiet=False):
        """Merge a tag file (or a JSON export) into the tags: rows already tagged get the tags
        of both and the newer note."""
        store = self.tags.store
        if store is None:
            return None
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="Load tags",
                                              filetypes=[("Tag file (JSON)", "*.json"),
                                                         ("All files", "*.*")])
        if not path:
            return None
        try:
            merge = getattr(self.tags, "merge_file", None) or store.merge_file
            added, merged = merge(path)
        except (OSError, ValueError, TagError) as e:
            messagebox.showerror("Load tags", "Cannot load %s:\n%s" % (path, e), parent=self)
            return None
        self.tags.changed()
        if not quiet:
            messagebox.showinfo("Load tags", "%d row%s added, %d already tagged row%s updated."
                                % (added, "" if added == 1 else "s", merged,
                                   "" if merged == 1 else "s"), parent=self)
        return added, merged

    # -- export ---------------------------------------------------------------------------------------
    def scope_entries(self, scope, tag=None):
        """(entries, description) for an export scope: 'all', 'filtered' (the rows this list
        keeps) or 'tag'."""
        store = self.tags.store
        if store is None:
            return [], ""
        if scope == "filtered":
            entries = self.filtered_entries()
            return entries, "rows shown in the Tagged tab (%d of %d)" % (len(entries),
                                                                         self._total())
        if scope == "tag" and tag:
            return self._entries(tag), "tag '%s'" % tag
        return self._entries(), "all tagged rows"

    def export(self, fmt, target, scope="all", tag=None, layout="tag", on_done=None):
        """Export without dialogs (the target was checked): returns the running job."""
        entries, what = self.scope_entries(scope, tag)
        return self.tags.export(fmt, target, entries, layout, what, on_done)

    def export_dialog(self):
        if self.tags.store is not None:
            return ExportDialog(self)
        return None


class ManageTagsDialog(tk.Toplevel):
    """Add, rename, recolour, delete and reorder tags (the order is the colour priority of a
    row with several tags, and Ctrl+1..9)."""

    def __init__(self, tab):
        tk.Toplevel.__init__(self, tab)
        self.tab, self.tags = tab, tab.tags
        self.title("Manage tags")
        self.configure(bg=C["bg"])
        self.transient(tab.winfo_toplevel())
        fit_geometry(self, 420, 360)
        ttk.Button(self, text="Close", command=self.destroy).pack(side="bottom", anchor="e",
                                                                  padx=8, pady=8)
        self.search = SearchBox(self, placeholder="Find a tag…", delay=0, find_button=False,
                                primary=True, width=24)
        self.search.pack(fill="x", padx=8, pady=(8, 0))
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree = ttk.Treeview(body, columns=("count",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="Tag")
        self.tree.heading("count", text="Rows")
        self.tree.column("#0", width=240)
        self.tree.column("count", width=70, anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(body, bg=C["bg"])
        bar.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (("Add\u2026", self.add), ("Rename\u2026", self.rename),
                          ("Colour\u2026", self.recolor), ("Delete", self.delete),
                          ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1))):
            ttk.Button(bar, text=text, command=cmd).pack(fill="x", pady=2)
        self.tree.bind("<Double-1>", lambda e: self.rename())
        self.filter = TreeFilter(self.tree, self.search, "tag", "tags")
        self.reload()

    def reload(self, select=None):
        store = self.tags.store
        self.tree.delete(*self.tree.get_children())
        if store is None:
            return
        counts = self.tab._counts()
        for d in store.defs:
            self.tree.insert("", "end", iid=d.name, text=" " + d.name,
                             image=self.tags.swatch(d.color),
                             values=(format(counts.get(d.name, 0), ","),))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)

    def chosen(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _op(self, op, *args):
        """A change of the tag definitions: in every open database's tags when there are
        several (Tagging.def_op), else in the one store."""
        fn = getattr(self.tags, "def_op", None)
        if fn is not None:
            return fn(op, *args)
        return getattr(self.tags.store, op)(*args)

    def _done(self, select=None):
        self.tags.changed()
        self.reload(select)

    def _error(self, e):
        messagebox.showerror("Manage tags", str(e), parent=self)

    def add(self, name=None, color=None):
        store = self.tags.store
        if name is None:
            name = simpledialog.askstring("New tag", "Name of the new tag:", parent=self)
        if not name:
            return
        try:
            d = self._op("add_def", name, color)
        except TagError as e:
            return self._error(e)
        self._done(d.name)

    def rename(self, new=None):
        old = self.chosen()
        if old is None:
            return
        if new is None:
            new = simpledialog.askstring("Rename tag", "New name for '%s':" % old,
                                         initialvalue=old, parent=self)
        if not new or new == old:
            return
        try:
            self._op("rename_def", old, new)
        except TagError as e:
            return self._error(e)
        self._done(new.strip())

    def recolor(self, color=None):
        name = self.chosen()
        if name is None:
            return
        store = self.tags.store
        if color is None:
            color = colorchooser.askcolor(color=store.color_of(name), parent=self,
                                          title="Colour of '%s'" % name)[1]
        if color:
            self._op("recolor_def", name, color)
            self._done(name)

    def delete(self, confirm=True):
        name = self.chosen()
        store = self.tags.store
        if name is None:
            return
        n = self.tab._counts().get(name, 0)
        if confirm and n and not messagebox.askyesno(
                "Delete tag", "Delete the tag '%s'? It is taken off %d row%s; rows left without "
                "a tag are no longer tagged (their notes go with them)." % (
                    name, n, "" if n == 1 else "s"), parent=self):
            return
        self._op("delete_def", name)
        self._done()

    def move(self, delta):
        name = self.chosen()
        if name is not None:
            self._op("move_def", name, delta)
            self._done(name)


class ExportDialog(tk.Toplevel):
    """Choose the format (HTML / CSV folder / JSON), the rows (all, the rows shown, one tag) and
    the HTML layout, then where to write it."""

    def __init__(self, tab):
        tk.Toplevel.__init__(self, tab)
        self.tab, self.tags = tab, tab.tags
        store = self.tags.store
        self.title("Export tagged rows")
        self.configure(bg=C["bg"])
        self.transient(tab.winfo_toplevel())
        self.resizable(False, False)
        pad = dict(padx=12, sticky="w")
        self.fmt = tk.StringVar(value="html")
        self.scope = tk.StringVar(value="all")
        self.layout = tk.StringVar(value="tag")
        self.tag = tk.StringVar(value=tab.selected_tag() or (store.defs[0].name
                                                             if store.defs else ""))
        tk.Label(self, text="Format", bg=C["bg"], font=F["body_bold"]).grid(
            row=0, column=0, pady=(10, 2), **pad)
        formats = (("html", "HTML report (one file, printable)"),
                   ("csv", "CSV folder (index, one file per table, BLOB files)"),
                   ("json", "JSON (can be loaded again with Load tags)"))
        for r, (value, text) in enumerate(formats, 1):
            ttk.Radiobutton(self, text=text, value=value, variable=self.fmt).grid(
                row=r, column=0, columnspan=2, **pad)
        tk.Label(self, text="Rows", bg=C["bg"], font=F["body_bold"]).grid(
            row=4, column=0, pady=(10, 2), **pad)
        shown = len(tab.filtered_entries())
        ttk.Radiobutton(self, text="All tagged rows (%s)" % format(tab._total(), ","),
                        value="all", variable=self.scope).grid(row=5, column=0, columnspan=2, **pad)
        ttk.Radiobutton(self, text="Rows shown in the Tagged tab (%s)" % format(shown, ","),
                        value="filtered", variable=self.scope).grid(row=6, column=0,
                                                                    columnspan=2, **pad)
        ttk.Radiobutton(self, text="One tag:", value="tag", variable=self.scope).grid(
            row=7, column=0, **pad)
        SearchableCombobox(self, textvariable=self.tag, values=store.names(), state="readonly",
                     width=20).grid(row=7, column=1, sticky="w", padx=(0, 12))
        tk.Label(self, text="HTML layout", bg=C["bg"], font=F["body_bold"]).grid(
            row=8, column=0, pady=(10, 2), **pad)
        ttk.Radiobutton(self, text="By tag, then table", value="tag",
                        variable=self.layout).grid(row=9, column=0, columnspan=2, **pad)
        ttk.Radiobutton(self, text="By table", value="table",
                        variable=self.layout).grid(row=10, column=0, columnspan=2, **pad)
        bar = tk.Frame(self, bg=C["bg"])
        bar.grid(row=11, column=0, columnspan=2, sticky="e", padx=12, pady=12)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Export\u2026", style="P.TButton", command=self.go).pack(
            side="right", padx=6)

    def go(self):
        fmt, scope, tag = self.fmt.get(), self.scope.get(), self.tag.get()
        entries, _what = self.tab.scope_entries(scope, tag)
        if not entries:
            messagebox.showinfo("Export", "There are no rows to export.", parent=self)
            return
        base = safe_filename(os.path.splitext(os.path.basename(self.tags.store.db_path))[0], 50)
        if fmt == "csv":
            target = filedialog.askdirectory(parent=self, title="Folder for the CSV files")
        else:
            ext = "." + fmt
            target = filedialog.asksaveasfilename(
                parent=self, title="Export tagged rows", defaultextension=ext,
                initialfile="%s-tagged%s" % (base, ext),
                filetypes=[(fmt.upper(), "*" + ext), ("All files", "*.*")])
        if not write_allowed(target):
            return
        self.destroy()

        def done(result):
            if result is None:
                return
            if fmt == "csv":
                msg = "Exported %d rows to %s\n%d BLOB file(s)%s" % (
                    result["rows"], target, result["blobs"],
                    ("; %d could not be written, e.g.\n%s" % (len(result["blob_errors"]),
                                                             result["blob_errors"][0]))
                    if result["blob_errors"] else "")
            else:
                msg = "Exported %d rows to\n%s" % (len(entries), result)
            messagebox.showinfo("Export", msg, parent=self.tab)
        self.tab.export(fmt, target, scope, tag, self.layout.get(), on_done=done)
