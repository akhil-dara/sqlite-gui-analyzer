"""BLOB inspector: the decoded structure of a BLOB next to its bytes.

The left side shows the decoded tree (property lists, keyed archives, protobuf, compressed
and encoded layers, text). Nested buffers are decoded again, so a gzip holding a protobuf
holding a bplist opens level by level. The right side shows the bytes of the buffer the
selected value lives in, with that value's bytes highlighted; clicking a byte selects the
value it belongs to. Numbers can be read as timestamps in every common epoch.

Decoding runs on a worker thread, so a large BLOB never freezes the window.
"""

import base64
import binascii
import io
import json
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from combobox import SearchableCombobox
from constants import C, HAS_PIL, _EXT_MAP
from tokens import COLOR as K, FONT as F
if HAS_PIL:
    from constants import PILImage, ImageTk
from engine.decode import decode_blob, interpretations, summary
from engine.decode import timestamps
from engine.decode.nodes import FAILED
from engine.decode.render import (format_of, json_text, plist_xml, protobuf_text,
                                  _plist_safe)  # noqa: F401 - kept importable from here
from hexview import HexView
import previews
from utils import blob_type, fmtb, is_image, write_allowed
from widgets import (SearchBox, TIP_LINES, TextFind, ToolTip, TreeFilter, cut_list,
                     fit_geometry)

BEST = "Best interpretation"
VALUE_CHARS = 300               # value column preview
VIEWS = (("auto", "Auto"), ("json", "JSON"), ("xml", "XML plist"), ("protobuf", "Protobuf"),
         ("text", "Text"))
_VIEW_NAMES = {"json": "JSON", "xml": "XML", "protobuf": "protobuf fields", "text": "text"}
_FORMAT_NAMES = {"protobuf": "protobuf", "json": "JSON", "typedstream": "a typedstream",
                 "text": "text"}


def _protobuf_of(node):
    """The protobuf message node is (or the first reading of its bytes), or None."""
    if node.kind == "protobuf":
        return node
    if node.kind == "bytes":
        return next((c for c in node.children if c.kind == "protobuf"), None)
    return None
TEXT_LIMIT = 1 << 20            # text tab shows at most this many characters


def node_value_text(node):
    """Short text for the Value column."""
    v = node.value
    k = node.kind
    if k == "bytes":
        return "%s" % fmtb(len(v or b""))
    if k in ("dict", "array", "object"):
        n = len(node.children)
        what = "keys" if k == "dict" else "fields" if k == "object" else "items"
        return ("%s (%d %s)" % (v, n, what)) if v else "(%d %s)" % (n, what)
    if k in ("protobuf",):
        return "(%d fields)" % len(node.children)
    if k == "image":
        return "%s %s" % (v, node.note)
    if v is None:
        return "" if k != "null" else "null"
    if isinstance(v, (bytes, bytearray)):
        return "<%d bytes>" % len(v)
    s = str(v)
    s = s.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return s if len(s) <= VALUE_CHARS else s[:VALUE_CHARS] + "\u2026"


def node_label(node, index):
    if node.label is not None:
        return str(node.label)
    return node.kind if index is None else "[%d] %s" % (index, node.kind)


def node_type_text(node):
    return node.kind + ("?" if node.confidence == "uncertain" else "")


def number_of(node):
    """The node's value as a number for timestamp decoding, or None."""
    v = node.value
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if any(c in v for c in ".eE") else int(v)
        except ValueError:
            return None
    if node.kind == "bytes" and isinstance(v, (bytes, bytearray)) and len(v) in (4, 8):
        return int.from_bytes(bytes(v), "little")
    return None


class DecodedTree(object):
    """Parent links and buffer ownership for a decoded tree (built once, then read-only)."""

    def __init__(self, root):
        self.root = root
        self.parent = {}                 # id(node) -> parent node
        self.nodes = {}                  # id(node) -> node (keeps ids valid)
        for node in root.walk():
            self.nodes[id(node)] = node
            for child in node.children:
                self.parent[id(child)] = node

    def buffer_of(self, node):
        """The buffer to show for `node`: its own for a "bytes" node (what it holds, which its
        children's offsets point into), else that of its nearest "bytes" ancestor."""
        p = node
        while p is not None and p.kind != "bytes":
            p = self.parent.get(id(p))
        return p if p is not None else self.root

    def path(self, node):
        out = []
        while node is not None:
            out.append(node)
            node = self.parent.get(id(node))
        return list(reversed(out))

    def node_at(self, buffer_node, offset):
        """Deepest node located in buffer_node's buffer that covers `offset`."""
        best, best_len = None, None
        stack = list(buffer_node.children)
        while stack:
            n = stack.pop()
            if n.offset is not None and n.length and n.offset <= offset < n.offset + n.length:
                if best_len is None or n.length <= best_len:
                    best, best_len = n, n.length
            if n.kind != "bytes":        # a nested buffer's children use its own coordinates
                stack.extend(n.children)
        return best


class ImagePane(ttk.Frame):
    """Image preview with fit/zoom (full formats with Pillow; PNG/GIF with Tk alone)."""

    refused = ""            # why the image is not previewed (limits 'preview_pixels', ...)

    def __init__(self, parent, data):
        ttk.Frame.__init__(self, parent)
        self._pil = None
        self._tk_img = None
        self._zoom = 1.0
        if HAS_PIL:
            try:
                self._pil = previews.open_image(data)
            except previews.PreviewRefused as e:
                self.refused = str(e)
                ttk.Label(self, text=self.refused).pack(padx=20, pady=20)
                self._pil = None
                return
            except Exception as e:     # noqa: BLE001 - shown to the user
                ttk.Label(self, text="Cannot load image: %s" % e).pack(padx=20, pady=20)
                self._pil = None
                return
            fmt = self._pil.format or "Unknown"
            w, h = self._pil.size
            ttk.Label(self, text="Format: %s  |  Size: %dx%d  |  Mode: %s" % (fmt, w, h, self._pil.mode),
                      style="M.TLabel").pack(fill="x", padx=4, pady=2)
            ctrl = ttk.Frame(self)
            ctrl.pack(fill="x", padx=4, pady=2)
            for text, cmd in (("Fit", self.fit), ("100%", lambda: self.set_zoom(1.0)),
                              ("Zoom +", lambda: self.set_zoom(self._zoom * 1.25)),
                              ("Zoom -", lambda: self.set_zoom(self._zoom / 1.25))):
                ttk.Button(ctrl, text=text, style="Sm.TButton", command=cmd).pack(side="left", padx=2)
            self._zoom_lbl = ttk.Label(ctrl, text="100%", style="M.TLabel")
            self._zoom_lbl.pack(side="left", padx=8)
            box = ttk.Frame(self)
            box.pack(fill="both", expand=True)
            self._canvas = tk.Canvas(box, bg=C["bg3"], highlightthickness=0)
            xsb = ttk.Scrollbar(box, orient="horizontal", command=self._canvas.xview)
            ysb = ttk.Scrollbar(box, orient="vertical", command=self._canvas.yview)
            self._canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
            ysb.pack(side="right", fill="y")
            xsb.pack(side="bottom", fill="x")
            self._canvas.pack(fill="both", expand=True)
            self._canvas.bind("<MouseWheel>",
                              lambda e: self.set_zoom(self._zoom * (1.1 if e.delta > 0 else 0.9)))
            self.after(100, self.fit)
        elif data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"GIF8":
            try:
                size = previews.header_size(data)
                if size is None:
                    raise previews.PreviewRefused("not previewed: no image size in the header")
                previews.check_size(*size)
                self._tk_img = tk.PhotoImage(data=data)
                ttk.Label(self, image=self._tk_img).pack(padx=10, pady=10)
            except previews.PreviewRefused as e:
                self.refused = str(e)
                ttk.Label(self, text=self.refused).pack(padx=20, pady=20)
            except tk.TclError:
                ttk.Label(self, text="Install Pillow for full image support:\npip install Pillow",
                          foreground=C["text2"]).pack(padx=20, pady=20)
        else:
            ttk.Label(self, text="Install Pillow for JPEG/WEBP image support:\npip install Pillow",
                      foreground=C["text2"]).pack(padx=20, pady=20)

    def fit(self):
        if not self._pil:
            return
        self._canvas.update_idletasks()
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)
        iw, ih = self._pil.size
        self.set_zoom(min(cw / float(iw), ch / float(ih), 1.0))

    def set_zoom(self, z):
        if not self._pil:
            return
        self._zoom = max(0.05, min(z, 10.0))
        iw, ih = self._pil.size
        nw, nh = previews.zoomed_size(iw, ih, self._zoom)
        self._zoom = nw / float(iw)
        resample = getattr(PILImage, "LANCZOS", getattr(PILImage, "BILINEAR", 2))
        try:
            self._tk_img = ImageTk.PhotoImage(self._pil.resize((nw, nh), resample))
        except MemoryError:
            self._zoom_lbl.configure(text="not enough memory for this zoom")
            return
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._tk_img)
        self._canvas.configure(scrollregion=(0, 0, nw, nh))
        self._zoom_lbl.configure(text="%d%%" % int(self._zoom * 100))


class BlobInspector(tk.Toplevel):
    """Inspector window for one BLOB. `context` names where it came from (table/column/row)."""

    def __init__(self, parent, data, col_name="BLOB", context=""):
        tk.Toplevel.__init__(self, parent)
        self._data = bytes(data or b"")
        self._context = context or col_name
        self.title("BLOB Inspector — %s (%s)" % (self._context, fmtb(len(self._data))))
        fit_geometry(self, 1100, 680)
        self.configure(bg=C["bg"])
        self._tree_model = None          # DecodedTree shown now
        self._attempts = []              # (label, Node) choices of the interpretation box
        self._iid_node = {}
        self._node_iid = {}
        self._buffer_shown = None
        self._result = None
        self._build()
        self._start_decode()

    # -- layout ----------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(6, 2))
        self._summary_lbl = ttk.Label(top, text="Decoding...", font=F["heading"])
        self._summary_lbl.pack(side="left")
        ttk.Label(top, text="  %s  |  %s" % (self._context, fmtb(len(self._data))),
                  style="M.TLabel").pack(side="left")

        btns = ttk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Button(btns, text="Save BLOB…", command=self._save_blob).pack(side="left", padx=3)
        ttk.Button(btns, text="Save decoded JSON…", command=self._save_json).pack(side="left", padx=3)
        ttk.Button(btns, text="Copy value", command=self._copy_value).pack(side="left", padx=3)
        ttk.Button(btns, text="Copy hex", command=self._copy_hex).pack(side="left", padx=3)
        ttk.Button(btns, text="Copy base64", command=self._copy_b64).pack(side="left", padx=3)
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right", padx=3)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=6, pady=2)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=2)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 3))
        ttk.Label(bar, text="Show:").pack(side="left")
        self._interp = SearchableCombobox(bar, state="readonly", width=38, values=[BEST])
        self._interp.set(BEST)
        self._interp.pack(side="left", padx=4)
        self._interp.bind("<<ComboboxSelected>>", lambda e: self._on_interpretation())
        self._failed_lbl = ttk.Label(bar, text="", style="M.TLabel")
        self._failed_lbl.pack(side="left", padx=4)
        self._failed_tip = ToolTip(self._failed_lbl, "")
        self._find_var = tk.StringVar()
        find = ttk.Entry(bar, textvariable=self._find_var, width=18)
        find.pack(side="right")
        find.bind("<Return>", lambda e: self._find_next())
        ToolTip(find, "Find a key or value in the decoded tree (Enter: next)")
        ttk.Label(bar, text="Find:").pack(side="right", padx=(8, 2))

        self._path_lbl = ttk.Label(left, text="", style="M.TLabel", anchor="w")
        self._path_lbl.pack(fill="x")
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("value", "type", "where"), selectmode="browse")
        self.tree.heading("#0", text="Key / Field")
        self.tree.heading("value", text="Value")
        self.tree.heading("type", text="Type")
        self.tree.heading("where", text="Bytes")
        self.tree.column("#0", width=220, stretch=False)
        self.tree.column("value", width=320)
        self.tree.column("type", width=100, stretch=False)
        self.tree.column("where", width=90, stretch=False)
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self._note_lbl = ttk.Label(left, text="", style="M.TLabel", anchor="w", wraplength=600,
                                   justify="left")
        self._note_lbl.pack(fill="x", pady=(2, 0))

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        hex_tab = ttk.Frame(self.tabs)
        self.tabs.add(hex_tab, text="Hex")
        self._buffer_lbl = ttk.Label(hex_tab, text="", style="M.TLabel")
        self._buffer_lbl.pack(fill="x")
        goto = ttk.Frame(hex_tab)
        goto.pack(fill="x", pady=2)
        ttk.Label(goto, text="Offset:").pack(side="left")
        self._goto_var = tk.StringVar()
        ge = ttk.Entry(goto, textvariable=self._goto_var, width=12)
        ge.pack(side="left", padx=3)
        ge.bind("<Return>", lambda e: self._goto_offset())
        ToolTip(ge, "Go to an offset: 0x1f, 1fh or 31")
        self.hex = HexView(hex_tab, self._data, on_select=self._on_byte)
        self.hex.pack(fill="both", expand=True)

        text_tab = ttk.Frame(self.tabs)
        self.tabs.add(text_tab, text="Decoded")
        # the selected value in the form that suits its format (Auto), or as JSON, an XML
        # property list (plists only), protobuf fields (protobuf only) or plain text; the
        # choice is kept, with a find box over the text
        bar = ttk.Frame(text_tab)
        bar.pack(fill="x", pady=(2, 2))
        ttk.Label(bar, text="View as:").pack(side="left", padx=(0, 4))
        self.view_var = tk.StringVar(master=self, value=self._saved_view())
        self._view_buttons = {}
        for key, label in VIEWS:
            b = ttk.Radiobutton(bar, text=label, value=key, variable=self.view_var,
                                style="Segment.TRadiobutton", command=self._view_changed)
            b.pack(side="left")
            self._view_buttons[key] = b
        ToolTip(self._view_buttons["auto"], "Auto: XML for plists, fields for protobuf, "
                "JSON for JSON and other decoded values, text for text")
        ttk.Button(bar, text="Copy", command=self._copy_view).pack(side="right")
        self._view_note = ttk.Label(text_tab, text="", style="M.TLabel", anchor="w")
        self._view_note.pack(fill="x")
        self.text_find = SearchBox(text_tab, placeholder="Find in the decoded value\u2026",
                                   delay=150, width=30)
        self.text_find.pack(fill="x", pady=(0, 2))
        self.text = tk.Text(text_tab, font=F["mono"], wrap="word", bg=C["bg2"], fg=C["text"],
                            insertbackground=C["text"])
        tsb = ttk.Scrollbar(text_tab, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self._finder = TextFind(self.text, self.text_find)
        self._text_node = None

        time_tab = ttk.Frame(self.tabs)
        self.tabs.add(time_tab, text="Timestamp")
        self._time_lbl = ttk.Label(time_tab, text="Select a number to read it as a date.",
                                   style="M.TLabel")
        self._time_lbl.pack(fill="x", padx=4, pady=4)
        self.times_find = SearchBox(time_tab, placeholder="Find a reading (kind or date)…",
                                    delay=100, width=30)
        self.times_find.pack(fill="x", padx=4, pady=(0, 4))
        self.times = ttk.Treeview(time_tab, columns=("kind", "utc"), show="headings", height=14)
        self.times.heading("kind", text="Read as")
        self.times.heading("utc", text="UTC")
        self.times.column("kind", width=200)
        self.times.column("utc", width=230)
        self.times.tag_configure("likely", background=K["success_soft"])
        self.times.pack(fill="both", expand=True, padx=4)
        self.times_filter = TreeFilter(self.times, self.times_find, "reading", "readings")

        self._image_tab = None
        if is_image(self._data):
            self._image_tab = ImagePane(self.tabs, self._data)
            self.tabs.add(self._image_tab, text="Image")

    # -- decoding --------------------------------------------------------------
    def _start_decode(self):
        data = self._data

        def work():
            try:
                self._result = (decode_blob(data), interpretations(data), summary(data))
            except Exception as e:     # noqa: BLE001 - the decoders never raise; be safe anyway
                self._result = e
        self._worker = threading.Thread(target=work, name="blob-decode", daemon=True)
        self._worker.start()
        self._poll_decode()

    def _poll_decode(self):
        if not self.winfo_exists():
            return
        if self._result is None:
            self.after(40, self._poll_decode)
            return
        if isinstance(self._result, Exception):
            self._summary_lbl.configure(text="Could not decode: %s" % self._result)
            return
        root, attempts, text = self._result
        self._summary_lbl.configure(text=text)
        self._attempts = [(BEST, root)]
        failed = []
        for a in attempts:
            kind, conf, node, reason = a[0], a[1], a[2], a[3]
            if conf == FAILED or node is None:
                failed.append("%s: %s" % (kind, reason))
                continue
            label = "%s (%s)" % (kind, conf) + ("" if getattr(a, "primary", True) else " - raw view")
            self._attempts.append((label, node))
        self._interp.configure(values=[a[0] for a in self._attempts])
        if failed:
            self._failed_lbl.configure(text="%d not matched" % len(failed))
            self._failed_tip.text = cut_list(failed, TIP_LINES)
        self.show_tree(root)

    def _on_interpretation(self):
        label = self._interp.get()
        for name, node in self._attempts:
            if name == label:
                if name == BEST:
                    self.show_tree(node)
                else:          # an interpretation of the whole BLOB: put it under a root buffer
                    root = type(node)("bytes", value=self._data, children=[node], offset=0,
                                      length=len(self._data))
                    self.show_tree(root)
                return

    def show_tree(self, root):
        self._tree_model = DecodedTree(root)
        self.tree.delete(*self.tree.get_children())
        self._iid_node, self._node_iid = {}, {}
        iid = self._insert("", root, None)
        self.tree.item(iid, open=True)
        self._fill(iid)
        kids = self.tree.get_children(iid)
        for child in kids:                              # open the chosen interpretations too
            self.tree.item(child, open=True)
            self._fill(child)
        # a decoded BLOB (bplist, protobuf, JSON...) opens on its decoded value, in the view
        # the user chose last; a BLOB nothing decodes opens on its bytes
        first = kids[0] if kids else None
        node = self._iid_node.get(first) if first else None
        if node is not None and node.kind != "bytes":
            self.tree.selection_set(first)
            self.tree.focus(first)
            try:
                self.tabs.select(1)
            except tk.TclError:
                pass
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)

    def _insert(self, parent_iid, node, index):
        where = ""
        if node.offset is not None and node.length:
            where = "%x+%d" % (node.offset, node.length)
        text = "BLOB" if parent_iid == "" else node_label(node, index)
        iid = self.tree.insert(parent_iid, "end", text=text,
                               values=(node_value_text(node), node_type_text(node), where))
        self._iid_node[iid] = node
        self._node_iid[id(node)] = iid
        if node.children:
            self.tree.insert(iid, "end", text="\u2026")          # placeholder until opened
        return iid

    def _fill(self, iid):
        """Insert the children of the node at iid (once)."""
        node = self._iid_node.get(iid)
        kids = self.tree.get_children(iid)
        if node is None or not kids or kids[0] in self._iid_node:
            return
        self.tree.delete(*kids)
        indexed = node.kind in ("array", "bytes")
        for i, child in enumerate(node.children):
            self._insert(iid, child, i if indexed and child.label is None else None)

    def _on_open(self, _event=None):
        self._fill(self.tree.focus())

    def reveal(self, node):
        """Select `node` in the tree, inserting the path down to it as needed."""
        if self._tree_model is None:
            return
        for n in self._tree_model.path(node):
            iid = self._node_iid.get(id(n))
            if iid is None:
                return
            if n is not node:
                self._fill(iid)
                self.tree.item(iid, open=True)
        iid = self._node_iid[id(node)]
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)

    # -- selection -------------------------------------------------------------
    def selected_node(self):
        sel = self.tree.selection()
        return self._iid_node.get(sel[0]) if sel else None

    def _on_select(self):
        node = self.selected_node()
        if node is None or self._tree_model is None:
            return
        model = self._tree_model
        self._path_lbl.configure(text=" \u203a ".join(
            node_label(n, None) if i else "BLOB" for i, n in enumerate(model.path(node))))
        self._note_lbl.configure(text=node.note or "")
        buf = model.buffer_of(node)
        if buf is not self._buffer_shown:
            self._buffer_shown = buf
            self.hex.set_data(buf.value or b"")
            where = "the BLOB" if buf is model.root else " \u203a ".join(
                node_label(n, None) for n in model.path(buf)[1:])
            self._buffer_lbl.configure(text="Bytes of %s (%s)" % (where, fmtb(len(buf.value or b""))))
        if node is buf:
            self.hex.highlight(None, 0)
        elif node.offset is not None and node.length:
            self.hex.highlight(node.offset, node.length)
        else:
            self.hex.highlight(None, 0)
        self._show_text(node)
        self._show_times(node)

    def _on_byte(self, offset):
        model = self._tree_model
        if model is None or self._buffer_shown is None:
            return
        node = model.node_at(self._buffer_shown, offset)
        if node is not None:
            self.reveal(node)

    def view_for(self, node):
        """(view used, note) for node: the chosen view when it suits the node's format,
        else the Auto choice (XML is for plists only, Protobuf for protobuf only)."""
        path = self._tree_model.path(node) if self._tree_model is not None else [node]
        fmt = format_of(path)
        self._view_buttons["xml"].state(["!disabled"] if fmt == "plist" else ["disabled"])
        self._view_buttons["protobuf"].state(["!disabled"] if fmt == "protobuf"
                                             else ["disabled"])
        auto = {"plist": "xml", "protobuf": "protobuf", "text": "text"}.get(fmt, "json")
        if auto == "protobuf" and not _protobuf_of(node):
            auto = "json"               # a single field value inside a message
        chosen = self.view_var.get()
        if chosen == "auto":
            return auto, ""
        if chosen == "xml" and fmt != "plist":
            return auto, "XML is for plists: this value is %s, shown as %s" % (
                _FORMAT_NAMES.get(fmt, "not a plist"), _VIEW_NAMES[auto])
        if chosen == "protobuf" and (fmt != "protobuf" or not _protobuf_of(node)):
            return auto, "Protobuf fields are for protobuf messages: shown as %s" % \
                _VIEW_NAMES[auto]
        return chosen, ""

    def _show_text(self, node):
        self._text_node = node
        view, note = self.view_for(node)
        self._view_note.configure(text=note)
        try:
            if view == "text" and node.kind in ("string", "text"):
                body = str(node.value)
            elif view == "text" and node.kind == "bytes":
                body = bytes(node.value or b"")[:TEXT_LIMIT].decode("utf-8", "replace")
            elif view == "protobuf":
                body = protobuf_text(_protobuf_of(node))
            elif view == "xml":
                body = plist_xml(node.to_plain())
            elif view == "text":
                plain = node.to_plain()
                body = plain if isinstance(plain, str) else json_text(plain)
            else:
                body = json_text(node.to_plain())
        except Exception as e:     # noqa: BLE001 - shown instead of the text
            body = "(cannot render: %s)" % e
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body[:TEXT_LIMIT])
        if len(body) > TEXT_LIMIT:
            self.text.insert("end", "\n\n[shown: the first %s of %s characters]" % (
                format(TEXT_LIMIT, ","), format(len(body), ",")))
        self.text.configure(state="disabled")
        self._finder.apply()

    def _saved_view(self):
        app = self._root()
        settings = getattr(getattr(app, "tags", None), "settings", None)
        v = settings.get("blob_view") if isinstance(settings, dict) else None
        return v if v in dict(VIEWS) else "auto"

    def _view_changed(self):
        app = self._root()
        tags = getattr(app, "tags", None)
        if tags is not None and isinstance(getattr(tags, "settings", None), dict):
            tags.settings["blob_view"] = self.view_var.get()
            try:
                tags.save_settings()
            except Exception:           # noqa: BLE001 - a choice not kept is not fatal
                pass
        if self._text_node is not None:
            self._show_text(self._text_node)

    def _copy_view(self):
        self._copy(self.text.get("1.0", "end-1c"))

    def _show_times(self, node):
        self.times.delete(*self.times.get_children())
        value = number_of(node)
        if value is None:
            self._time_lbl.configure(text="Select a number to read it as a date.")
            return
        likely = set(k for k, _iso in timestamps.guess(value))
        self._time_lbl.configure(text="%s read as a date in each epoch, UTC (green: plausible, "
                                      "%d-%d)" % (value, timestamps.LO_YEAR, timestamps.HI_YEAR))
        for kind, label, _epoch, _unit in timestamps.KINDS:
            dt = timestamps.to_datetime(value, kind)
            if dt is not None:
                self.times.insert("", "end", values=(label, timestamps.fmt_utc(dt)),
                                  tags=("likely",) if kind in likely else ())

    # -- find / goto -------------------------------------------------------------
    def _find_next(self):
        needle = self._find_var.get().strip().lower()
        model = self._tree_model
        if not needle or model is None:
            return
        nodes = list(model.root.walk())
        current = self.selected_node()
        start = 0
        for i, n in enumerate(nodes):
            if n is current:
                start = i + 1
                break
        for n in nodes[start:] + nodes[:start]:
            hay = "%s %s" % (n.label if n.label is not None else "",
                             n.value if isinstance(n.value, (str, int, float)) else "")
            if needle in hay.lower():
                self.reveal(n)
                return
        self.bell()

    def _goto_offset(self):
        from hexview import parse_offset
        off = parse_offset(self._goto_var.get())
        if off is not None:
            self.hex.goto(off)
            self._on_byte(min(off, max(len(self.hex.data) - 1, 0)))

    # -- actions -------------------------------------------------------------
    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _copy_value(self):
        node = self.selected_node()
        if node is None:
            return
        v = node.value
        if isinstance(v, (bytes, bytearray)):
            self._copy(binascii.hexlify(bytes(v)).decode())
        elif node.children:
            self._copy(json.dumps(node.to_plain(), indent=2, ensure_ascii=False, default=str))
        else:
            self._copy("" if v is None else str(v))

    def _copy_hex(self):
        self._copy(binascii.hexlify(self._data).decode())

    def _copy_b64(self):
        self._copy(base64.b64encode(self._data).decode())

    def _save_blob(self):
        ext = _EXT_MAP.get(blob_type(self._data), ".bin")
        path = filedialog.asksaveasfilename(parent=self, defaultextension=ext,
                                            filetypes=[("All files", "*.*")])
        if write_allowed(path):
            try:
                with open(path, "wb") as f:
                    f.write(self._data)
            except OSError as e:
                messagebox.showerror("Error", str(e), parent=self)
                return
            self._written(path, "BLOB bytes of %s" % self._context)

    def _save_json(self):
        if self._tree_model is None:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if write_allowed(path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"source": self._context, "size": len(self._data),
                               "decoded": self._tree_model.root.to_plain()},
                              f, indent=2, ensure_ascii=False, default=str)
            except OSError as e:
                messagebox.showerror("Error", str(e), parent=self)
                return
            self._written(path, "decoded BLOB of %s" % self._context)

    def _written(self, path, source):
        """A file saved from the inspector: its manifest and the activity log (when the
        inspector belongs to the app)."""
        app = self._root()
        if hasattr(app, "activity") and hasattr(app, "case"):
            from jobs import file_written
            file_written(app, path, source, parent=self, title="Save")
