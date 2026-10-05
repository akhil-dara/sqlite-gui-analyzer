"""DataGrid: a virtual table widget that draws only the cells in view, for any number of rows
and columns (250 columns x 1,000,000 rows scroll like 10 x 100).

Data source protocol (duck-typed; browse_sources has the database and in-memory sources):

    columns()           -> list of column names. With frozen=1 the first column (the row
                           locator '_rid') stays in view while scrolling sideways.
    row_count()         -> int, or None while unknown: the grid then grows as rows arrive, and
                           a window shorter than asked for marks the end.
    rows(start, count)  -> list of (values, flags) for rows start .. start+count-1: values has
                           one value per column (None, int, float, str, bytes, InvalidText or
                           a Locator), flags a set of engine row flags (damaged_record,
                           pre_alter, extra_values, virtual_generated).
  optional:
    threaded            -> True when rows() is slow (a database read): it is then called on a
                           worker thread, never on the Tk thread while scrolling. Until the
                           source's first rows arrive they show a placeholder; after that the
                           rows drawn last stay in view until the new ones arrive.
    sort(column, desc)  -> order the rows by a column (the frozen column: natural order).
    set_filters(col_exprs, global_text)
                        -> {column: expression} in engine.filters syntax (only valid ones are
                           passed) and the global filter text; may raise FilterError.
    release_thread()    -> called on the worker thread when it has no more work, just before
                           it ends (e.g. to close its database connection).

    interrupt(thread)   -> stop the read running on the worker thread `thread` (a window the
                           view no longer needs, e.g. while the scrollbar is dragged).
    retryable(error)    -> True when a failed read was only cancelled and may be read again.

Rows are fetched in windows of limits 'grid_window_rows' rows; 'grid_cache_windows' windows
are kept (least recently used dropped), and 'grid_prefetch_windows' are read ahead in the
direction the view is scrolling. Every change of source, sort or filter starts a new
generation, and results of older fetches are dropped when they arrive. Worker threads never
call into Tk: results reach the Tk thread through a queue it polls with after().

The grid never shows empty rows for a source it has drawn rows of: while the rows at a new
position (after a scroll, a sort or a filter) are being read, it keeps the rows it drew last
(waiting() is True, target_row_range() says where the view is going, the scrollbar is
already there) and draws the new rows when they arrive. While the scrollbar is dragged, rows
not read yet are asked for once the thumb has rested 'grid_drag_debounce_ms'; a read for a
position left behind is stopped (the source's interrupt()).

Drawing: four canvases (corner, header, frozen column, cells) hold items at fixed positions and
scroll by moving their view origin, which costs nothing per item; only rows and columns that
come into view get items (reused from those that left it), so a scroll step draws a few cells.

Filters (colfilter.py): each column header has a funnel that opens the column's filter
popover (type-aware conditions, a checklist of its values with counts, a live count); a bar
above the grid holds the search of all columns (its words are highlighted in the cells), a
chip per filter in force, Back / Forward through the filters and sorts used (Ctrl+Z / Ctrl+Y),
Saved filters, Copy as SQL WHERE / filter text and the count. The filter row under the headers
(one expression field per column) is an optional compact mode (set_inline_filters). Optional
source methods used by the popover: column_types(), distinct_values(), count_matching(),
estimate_matching(), nth_value(), date_bins() (see browse_sources.TableSource); sources
without them are read row by row on the filter worker.

Saved filters: the owner sets grid.saved_filters = (load_fn, save_fn) with load_fn(key) ->
{name: filters} and save_fn(key, name, filters) (filters None deletes the name), and
grid.filter_key = the table's key (a string, or a function returning one; default: the
source's `table`). `filters` is JSON-safe: {"columns": {column: filter text}, "search": text,
"sort": [column, descending] or None}; apply_saved_filter() skips columns the table lacks.
After every change of the filters in force the grid sends <<GridFiltersChanged>>.
"""

import bisect
import collections
import io
import json
import re
import sys
import threading
import time
import tkinter as tk
import weakref
from tkinter import ttk
import tkinter.font as tkfont

import colfilter
from constants import C, ROW_FLAG_BG
from engine import limits
from engine import timeline as tl
from engine.backends import Filter
from engine.csvcells import csv_text, csv_writer, formula_safe
from engine.fileformat.record import InvalidText
from engine.filters import (FilterError, ascii_lower, check_expr, condition_text, parse_words,
                            value_expr)
from engine.schema import Locator
from engine.search import value_type
from tokens import COLOR as K, FONT as F, FAMILY
from utils import ROW_FLAGS, blob_type, json_value, plain_text, row_flag_tag, vb
from widgets import SearchBox, ToolTip, add_placeholder

MIN_COL_W = 36
MAX_AUTOSIZE_W = 600
REDRAW_BUDGET = 0.05     # seconds a redraw when idle may take before it goes on at the next turn
PAD = 6                 # text inset in a cell
BORDER_GRIP = 4         # pixels either side of a header border that start a resize
BAND = 60000            # rows either side of the base row that canvas coordinates may cover
REBASE = 20000          # re-place every item once the view is this many rows from the base
FILTER_HINT = ("Filter this column:\n"
               "  text   contains (any case)      !text   does not contain\n"
               "  >5  >=5  <5  <=5  =x  <>x      5~10  range\n"
               "  a%b_c   LIKE pattern            /regex/  or /regex/i\n"
               "  NULL   NOT NULL                 \"\" empty text\n"
               "Enter applies at once, Esc clears.")
GLOBAL_HINT = ("Filter all columns: keeps the rows where every word occurs in some column "
               "(any case). Each column also has its own filter under its header:")


def grid_search(parent, grid, placeholder="Find rows (words, all must match)…", **kw):
    """The one search field for a grid: keeps the rows where every word occurs in some
    column, as you type (the grid's filter of all columns); cleared with the grid's source."""
    kw.setdefault("tooltip", "Keeps the rows where every word occurs in some column, as you "
                             "type (matches are highlighted). Each column header's funnel "
                             "filters that column.")
    box = SearchBox(parent, placeholder=placeholder, delay=400, find_button=False,
                    width=kw.pop("width", 30),
                    on_change=lambda t: grid.set_global_filter(t, True),
                    on_next=lambda forward: grid.focus_set(), **kw)
    grid.use_search_box(box)
    return box


def show_filter_help(anchor):
    """The '?' beside a filter: the syntax of the filters in a small window under it."""
    top = anchor.winfo_toplevel()
    win = tk.Toplevel(top)
    win.title("Filter syntax")
    win.configure(bg=C["bg"])
    win.transient(top)
    tk.Label(win, text=GLOBAL_HINT, bg=C["bg"], fg=C["text"], font=F["body"],
             justify="left", wraplength=420).pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(win, text=FILTER_HINT.split("\n", 1)[1], bg=C["bg2"], fg=C["text"],
             font=F["mono"], justify="left", anchor="w").pack(fill="x", padx=10)
    ttk.Button(win, text="Close", command=win.destroy).pack(side="right", padx=10, pady=8)
    win.bind("<Escape>", lambda e: win.destroy())
    try:
        win.update_idletasks()
        win.geometry("+%d+%d" % (anchor.winfo_rootx(),
                                 anchor.winfo_rooty() + anchor.winfo_height() + 4))
    except tk.TclError:
        pass
    return win
_MISSING = object()


def _flat(s):
    return s.replace("\r\n", "\u21b5").replace("\n", "\u21b5").replace("\r", "\u21b5") \
            .replace("\t", " ")


_badges = {}


def tip_preview(s):
    """A readable part of a long text for a tooltip: its first limits 'cell_tip_lines' lines
    and 'cell_tip_chars' characters, line breaks kept, and what was left out."""
    max_chars, max_lines = limits.get("cell_tip_chars"), limits.get("cell_tip_lines")
    head = s[:max_chars]
    lines = head.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut_lines = len(lines) > max_lines
    lines = [ln.replace("\t", "    ") for ln in lines[:max_lines]]
    out = "\n".join(lines)
    if cut_lines or len(s) > max_chars:
        out += "\n… (%s more characters)" % format(max(0, len(s) - len(out)), ",")
    return out


def size_badge(v):
    """A short note drawn at the right of a cell whose text value is long or spans several
    lines ('↵ 42 lines', '12,345 chars'), else ''. Cached per value (texts can be MBs)."""
    if not isinstance(v, str):
        return ""
    limit = limits.get("cell_draw_chars")
    if len(v) <= limit and "\n" not in v and "\r" not in v:
        return ""
    hit = _badges.get(id(v))
    if hit is not None and hit[0] is v and hit[1] == limit:
        return hit[2]
    lines = v.count("\n") + 1 if "\n" in v else (v.count("\r") + 1 if "\r" in v else 1)
    parts = []
    if lines > 1:
        parts.append("↵ %s lines" % format(lines, ","))
    if len(v) > limit or lines > 1:
        parts.append("%s chars" % format(len(v), ","))
    badge = " · ".join(parts)
    if len(_badges) > 4000:
        _badges.clear()
    _badges[id(v)] = (v, limit, badge)
    return badge


_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BINARY_PREVIEW = 40                     # escaped characters shown of text holding binary data


def binary_text_cell(s):
    """'[binary text 120 chars · Protobuf?] \\x0a\\x05…' for text holding binary data
    (control characters other than tab and line breaks), or None for ordinary text."""
    if not _CONTROL.search(s, 0, 512):
        return None
    bt = blob_type(s[:4096].encode("utf-8", "surrogateescape"))
    shown = _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()),
                         s[:BINARY_PREVIEW].replace("\n", "\\x0a").replace("\r", "\\x0d")
                         .replace("\t", "\\x09"))
    return "[binary text %s chars%s] %s%s" % (
        format(len(s), ","), (" \u00b7 " + bt) if bt != "BLOB" else "", shown,
        "\u2026" if len(s) > BINARY_PREVIEW else "")


def cell_text(v, limit=None):
    """(text, kind) for drawing a value: kind is 'null', 'blob', 'invalid' or 'text'. Text is
    cut at `limit` characters (default limits 'cell_draw_chars'); size_badge() says how long
    it really is."""
    if limit is None:
        limit = limits.get("cell_draw_chars")
    if v is None:
        return "NULL", "null"
    if isinstance(v, InvalidText):
        return vb(v), "invalid"
    if isinstance(v, bytes):
        bt = blob_type(v)
        return "[BLOB %s bytes%s]" % (format(len(v), ","), (" \u00b7 " + bt) if bt != "BLOB" else ""), "blob"
    s = v if isinstance(v, str) else str(v)
    if isinstance(v, str):
        binary = binary_text_cell(v)
        if binary is not None:
            return binary, "blob"
    if len(s) > limit:
        s = s[:limit]
    if "\n" in s or "\r" in s or "\t" in s:
        s = _flat(s)
    return s, "text"


def tidy_menu(menu):
    """No separator first, last or twice in a row (a menu offers only what applies, so
    groups can come out empty)."""
    try:
        end = menu.index("end")
    except tk.TclError:
        return
    if end is None:
        return
    prev_sep = True                     # a separator first goes too
    i = 0
    while i <= end:
        sep = menu.type(i) == "separator"
        if sep and prev_sep:
            menu.delete(i)
            end -= 1
            continue
        prev_sep = sep
        i += 1
    if end >= 0 and menu.type(end) == "separator":
        menu.delete(end)


class Runner(object):
    """Runs jobs on a worker thread and hands their results to the Tk thread.

    submit(key, fn, done): fn() runs on the worker thread (the most recently submitted job
    first; a queued job with the same key is replaced). done(result, error) is then called on
    the Tk thread, from an after() poll, so no worker thread ever calls into Tk. The worker
    ends when it has nothing left to do, after calling release() on its own thread.
    """

    def __init__(self, widget, name="grid-worker", release=None, poll_ms=15):
        self.widget, self.name, self.release, self.poll_ms = widget, name, release, poll_ms
        self._lock = threading.Lock()
        self._jobs = collections.OrderedDict()
        self._results = collections.deque()
        self._thread = None
        self._threads = []
        self._running = None
        self._poll_id = None
        self._closed = False
        try:
            # the widget going away cancels the result poll (its Tcl command goes with it); a
            # main window cancels every timer itself when it is destroyed (App.destroy)
            if not isinstance(widget, tk.Tk):
                widget.bind("<Destroy>", self._on_destroy, add="+")
        except (AttributeError, tk.TclError):
            pass

    def _on_destroy(self, event):
        if event.widget is self.widget:
            self.close()

    def close(self):
        """No more polls: queued jobs are dropped, the pending poll is cancelled (the widget is
        being destroyed). A running job still ends on its own."""
        self._closed = True
        self.cancel()
        pid, self._poll_id = self._poll_id, None
        if pid is not None:
            try:
                self.widget.after_cancel(pid)
            except (tk.TclError, RuntimeError, ValueError):
                pass

    def submit(self, key, fn, done):
        self.submit_many([(key, fn, done)])

    def submit_many(self, jobs):
        """submit() several (key, fn, done) jobs at once; the last one runs first."""
        if not jobs:
            return
        with self._lock:
            for key, fn, done in jobs:
                self._jobs.pop(key, None)
                self._jobs[key] = (fn, done)
            if self._thread is None:
                th = threading.Thread(target=self._work, name=self.name)
                th.daemon = True
                self._thread = th
                self._threads = [t for t in self._threads if t.is_alive()] + [th]
                th.start()
        self._schedule_poll()

    def discard(self, keep):
        """Drop queued jobs whose key keep(key) rejects; returns their keys."""
        with self._lock:
            gone = [k for k in self._jobs if not keep(k)]
            for k in gone:
                del self._jobs[k]
        return gone

    def cancel(self):
        """Drop every queued job (a running one finishes; its result is still delivered)."""
        with self._lock:
            gone = list(self._jobs)
            self._jobs.clear()
        return gone

    def running_thread(self):
        """The thread running a job now, or None."""
        with self._lock:
            return self._thread if self._running is not None else None

    def running_key(self):
        """The key of the job running now, or None."""
        with self._lock:
            return self._running

    def running(self):
        """(key, thread) of the job running now, or (None, None)."""
        with self._lock:
            if self._running is None:
                return None, None
            return self._running, self._thread

    def threads(self):
        """Worker threads still alive (including one finishing its release())."""
        return [t for t in self._threads if t.is_alive()]

    def busy(self):
        with self._lock:
            return bool(self._jobs or self._running is not None or self._results)

    def stop(self, timeout=3.0):
        """Cancel queued jobs and wait up to `timeout` seconds for the workers to end."""
        self.cancel()
        deadline = time.time() + timeout
        for t in self.threads():
            t.join(max(0.0, deadline - time.time()))
        return self.threads()

    def _work(self):
        while True:
            with self._lock:
                if not self._jobs:
                    self._thread = None
                    break
                key, (fn, done) = self._jobs.popitem(last=True)
                self._running = key
            result, error = None, None
            try:
                result, error = fn(), None
            except Exception as e:      # noqa: BLE001 - handed to done() on the Tk thread
                e.__traceback__ = None  # its frames hold this thread's objects
                result, error = None, e
            except BaseException as e:  # noqa: BLE001 - still unblock the poll loop
                result, error = None, e
            finally:
                # BaseException (KeyboardInterrupt on a worker thread) must also clear
                # _running, or busy() stays true and the poll reschedules itself forever.
                with self._lock:
                    self._running = None
                    self._results.append((done, result, error))
            result = error = None
        if self.release is not None:
            try:
                self.release()
            except Exception:           # noqa: BLE001 - nothing to report it to
                pass

    def _schedule_poll(self):
        if self._poll_id is None and not self._closed:
            try:
                self._poll_id = self.widget.after(self.poll_ms, self._poll)
            except (tk.TclError, RuntimeError):
                self._poll_id = None

    def _poll(self):
        self._poll_id = None
        while True:
            with self._lock:
                if not self._results:
                    break
                done, result, error = self._results.popleft()
            try:
                done(result, error)
            except Exception:           # noqa: BLE001 - report it, deliver the others
                try:
                    self.widget.report_callback_exception(*sys.exc_info())
                except Exception:       # noqa: BLE001
                    pass
        if self.busy() or self._thread is not None:
            self._schedule_poll()


class _Keyed(object):
    """Canvas items of one kind, keyed by what they show (a row, a column, a cell).

    An item keeps its place while its key stays in view, so a redraw only configures items
    whose content changed and those of rows or columns that came into view. Items whose key
    left the view are hidden and reused.
    """
    __slots__ = ("cv", "kind", "tags", "opts", "items", "free", "frame", "grew")

    def __init__(self, cv, kind, tags, opts):
        self.cv, self.kind, self.tags, self.opts = cv, kind, tags, opts
        self.items = {}         # key -> [item id, state, frame last shown]
        self.free = []
        self.frame = 0
        self.grew = False

    def begin(self):
        self.frame += 1

    def put(self, key, coords, state):
        ent = self.items.get(key)
        if ent is not None:
            ent[2] = self.frame
            if ent[1] != state:
                self.cv.coords(ent[0], *coords)
                self.cv.itemconfigure(ent[0], **self.opts(state))
                ent[1] = state
            return
        if self.free:
            iid = self.free.pop()
            self.cv.coords(iid, *coords)
            self.cv.itemconfigure(iid, state="normal", **self.opts(state))
        else:
            iid = getattr(self.cv, "create_" + self.kind)(*coords, tags=self.tags,
                                                           **self.opts(state))
            self.grew = True
        self.items[key] = [iid, state, self.frame]

    def end(self):
        frame = self.frame
        gone = [k for k, ent in self.items.items() if ent[2] != frame]
        for k in gone:
            iid = self.items.pop(k)[0]
            self.cv.itemconfigure(iid, state="hidden")
            self.free.append(iid)

    def clear(self):
        for iid, _state, _frame in self.items.values():
            self.cv.itemconfigure(iid, state="hidden")
            self.free.append(iid)
        self.items = {}


_LAYERS = ("rowbg", "hl", "marker", "gridline", "cur", "cell", "hdrbg", "funnel", "hdrtext",
           "fixed")
MARKER_W = 4            # width of the row marker drawn at the left edge of the frozen column
FUNNEL_W = 16           # room for the filter funnel at the right of a column header
FUNNEL_MIN_COL = 44     # narrower columns draw no funnel (the header menu has Filter…)


def funnel_points(x, y):
    """A funnel whose top edge is centred at (x, y): a triangle over a short stem."""
    return (x - 5, y, x + 5, y, x + 1, y + 5, x + 1, y + 10, x - 1, y + 9, x - 1, y + 5)


class _Surface(object):
    """One canvas and its keyed items. The canvas scrolls by its view origin (scroll increment
    1 pixel, not confined), so items stay where they are placed."""

    def __init__(self, cv, fonts):
        def rect(state):
            return {"fill": state[0], "outline": state[1]}

        def line(state):
            return {"fill": state[0]}

        def text(state):
            return {"text": state[0], "fill": state[1], "font": fonts[state[2]], "anchor": "w"}

        def right(state):
            return {"text": state[0], "fill": state[1], "font": fonts[state[2]], "anchor": "e"}

        def poly(state):
            return {"fill": state[0], "outline": state[1]}
        self.cv = cv
        self.badge = _Keyed(cv, "text", ("cell",), right)
        self.rowbg = _Keyed(cv, "rectangle", ("rowbg",), rect)
        self.hl = _Keyed(cv, "rectangle", ("hl",), rect)
        self.funnel = _Keyed(cv, "polygon", ("funnel",), poly)
        self.marker = _Keyed(cv, "rectangle", ("marker",), rect)
        self.hline = _Keyed(cv, "line", ("gridline",), line)
        self.vline = _Keyed(cv, "line", ("gridline",), line)
        self.cell = _Keyed(cv, "text", ("cell",), text)
        self.hdrbg = _Keyed(cv, "rectangle", ("hdrbg",), rect)
        self.hdrtext = _Keyed(cv, "text", ("hdrtext",), text)
        self.fixed = _Keyed(cv, "text", ("fixed",), text)
        self.cur = cv.create_rectangle(0, 0, 0, 0, outline=C["accent"], width=2, state="hidden",
                                       tags=("cur",))
        self.cur_box = None
        self.all = (self.rowbg, self.hl, self.marker, self.hline, self.vline, self.cell,
                    self.badge, self.hdrbg, self.funnel, self.hdrtext, self.fixed)
        self.origin = (0, 0)
        self.layout = None

    def begin(self, layout, ox, oy):
        """Start a redraw with the view origin at (ox, oy); returns True when every item was
        dropped (the layout changed) and must be placed anew."""
        cleared = layout != self.layout
        if cleared:
            for k in self.all:
                k.clear()
            self.layout = layout
        dx, dy = ox - self.origin[0], oy - self.origin[1]
        if dx:
            self.cv.xview_scroll(dx, "units")
        if dy:
            self.cv.yview_scroll(dy, "units")
        self.origin = (ox, oy)
        for k in self.all:
            k.begin()
        return cleared

    def set_cur(self, box):
        if box != self.cur_box:
            if box is None:
                self.cv.itemconfigure(self.cur, state="hidden")
            else:
                self.cv.coords(self.cur, *box)
                self.cv.itemconfigure(self.cur, state="normal")
            self.cur_box = box

    def end(self):
        grew = False
        for k in self.all:
            k.end()
            grew = grew or k.grew
            k.grew = False
        if grew:
            for name in _LAYERS:
                self.cv.tag_raise(name)


_FONTS = {}


def grid_fonts(widget):
    """The four fonts every grid of a Tk interpreter shares (body, italic, bold, small).
    Shared, never deleted: a Font freed by the garbage collector on a worker thread (a grid
    left in a reference cycle) would wait for the Tk thread to delete it, stalling the
    worker's reads for a second or more."""
    key = widget.tk
    fonts = _FONTS.get(key)
    if fonts is None:
        root, family = widget._root(), FAMILY
        fonts = _FONTS[key] = (tkfont.Font(root, family=family, size=9),
                               tkfont.Font(root, family=family, size=9, slant="italic"),
                               tkfont.Font(root, family=family, size=9, weight="bold"),
                               tkfont.Font(root, family=family, size=8))
    return fonts


_CALIBRATE = "The quick brown fox jumps over the lazy dog. AV To Wa ff fi 0123456789 {|}~"
# one character of each script whose fallback font Tk looks up the first time it is measured
# or drawn (up to half a second for emoji: every installed font is searched)
FALLBACK_SAMPLE = ("\U0001F600\u2764\u0928\u0915\u0c05\u0b85\u0985\u4f60\u3042\uac00"
                   "\u0627\u05d0\u0e01\u0431\u03b1\u2713")
MEASURE_BUDGET = 0.012      # seconds of the Tk thread per step measuring new characters


class CharWidths(object):
    """The width in pixels of each character met so far, per grid font, shared by the grids
    of a Tk interpreter (like the fonts).

    Text is cut to fit a cell by adding up its characters' widths, instead of asking Tk to
    measure whole strings (a cell cut to fit took ~8 measure calls). A character outside
    Latin-1 not measured yet counts as wide as the widest (the cut is then a little early,
    never too late) and is measured in short steps on the Tk thread afterwards: Tk measures
    such characters slowly (1-20 ms each, emoji and scripts the font lacks need a fallback
    font). The grids then fit their text again."""

    _all = {}           # Tk interpreter -> CharWidths

    @classmethod
    def of(cls, widget):
        key = widget.tk
        cw = cls._all.get(key)
        if cw is None:
            cw = cls._all[key] = cls(widget)
        return cw

    def __init__(self, widget):
        self.fonts = grid_fonts(widget)
        self.root = widget._root()
        self.tables = tuple({} for _f in self.fonts)
        self.exact = []
        for f, table in zip(self.fonts, self.tables):
            for ch in set(_CALIBRATE):
                table[ch] = f.measure(ch)
            # a font system that kerns ASCII text: every cut is checked against Tk instead
            self.exact.append(sum(table[ch] for ch in _CALIBRATE) == f.measure(_CALIBRATE))
        self.guess = [max(f.measure(ch) for ch in "WM@\u5b57") + 1 for f in self.fonts]
        self.pending = collections.OrderedDict()    # (font index, character) waiting
        self.grids = weakref.WeakSet()              # told when widths were learnt
        self.gen = 0                                # bumped when widths were learnt
        self._after = None

    def width(self, ch, fi):
        """The character's width, measured now (Latin-1) or guessed and queued."""
        table = self.tables[fi]
        if ch <= "\xff":
            if len(table) > 50000:
                table.clear()
            w = table[ch] = self.fonts[fi].measure(ch)
            return w
        self.pending[(fi, ch)] = None
        if self._after is None:
            try:
                self._after = self.root.after(30, self._measure_some)
            except tk.TclError:
                self._after = None
        return self.guess[fi]

    def _measure_some(self):
        self._after = None
        t0 = time.perf_counter()
        learnt = False
        while self.pending and time.perf_counter() - t0 < MEASURE_BUDGET:
            (fi, ch), _v = self.pending.popitem(last=False)
            table = self.tables[fi]
            if ch not in table:
                try:
                    table[ch] = self.fonts[fi].measure(ch)
                except tk.TclError:
                    return
                learnt = True
        if learnt:
            self.gen += 1
            for g in list(self.grids):
                g.widths_learnt()
        if self.pending:
            try:
                self._after = self.root.after(30, self._measure_some)
            except tk.TclError:
                self._after = None

    def measure_now(self):
        """Measure every character waiting (tests)."""
        while self.pending:
            if self._after is not None:
                try:
                    self.root.after_cancel(self._after)
                except tk.TclError:
                    pass
                self._after = None
            self._measure_some()


def warm_fallback_fonts(widget):
    """Let Tk find its fallback fonts for emoji and the common non-Latin scripts now, once
    (App calls this at start, while its window is still invisible): otherwise the first
    table showing such text pauses for up to half a second while every installed font is
    searched."""
    cw = CharWidths.of(widget)
    for fi, f in enumerate(cw.fonts):
        f.measure(FALLBACK_SAMPLE)
        for ch in FALLBACK_SAMPLE:
            if fi == 0:
                cw.tables[fi][ch] = f.measure(ch)


class DataGrid(tk.Frame):
    """Virtual grid over a data source (see the module docstring).

    Callbacks (all optional):
      on_open_row(row, values)            double-click / Enter on a row
      on_open_blob(row, column, value)    'Inspect BLOB…' (the BLOB Inspector)
      on_sort(column, desc)               after a header click re-sorted the source
      on_filter(col_exprs, global_text)   after the filters changed (valid expressions only)
      on_view_change(grid)                after a redraw showed other rows or new data
      describe(value) -> str              extra 'Decoded' text in the row inspector
      row_style(row, values, flags) -> (background, marker)
                                          how to mark a loaded row in view (called on every
                                          redraw, so keep it cheap): a background colour for
                                          the whole row and a marker colour for a small bar at
                                          the left edge of the frozen column, each None for
                                          none. The selection and the engine's row-flag tints
                                          (damaged rows) take precedence over the background;
                                          the marker is always drawn, so a mark stays visible
                                          on a selected or flagged row. Call restyle() after
                                          what it returns changed.
      on_context_menu(menu, row, col)     after the right-click menu of a cell was built, to
                                          add entries to it (the menu is not posted yet)
      on_header_menu(menu, col)           the same for the right-click menu of a header

    Options: search=True puts a 'Search in rows…' box in the filter bar (False when the owner
    has its own box: it calls set_global_filter, or grid_search() which hides this one);
    inline_filters=True: the filter row under the headers (set_inline_filters toggles it).

    Column formatters (set_column_formatter) change how a column's values are drawn:
    formatter(value) -> text, or None to draw the value itself. The source, sorting, filters,
    row copies and exports keep the raw values; the cell tooltip and the inspector show both,
    and the cell menu offers 'Copy raw value'.
    """

    def __init__(self, master, frozen=1, on_open_row=None, on_open_blob=None, on_sort=None,
                 on_filter=None, on_view_change=None, on_selection=None, describe=None,
                 filter_delay=300,
                 row_style=None, on_context_menu=None, on_header_menu=None, search=True,
                 inline_filters=True, **kw):
        kw.setdefault("bg", C["bg"])
        tk.Frame.__init__(self, master, **kw)
        self._inline = bool(inline_filters)
        self.saved_filters = None       # (load_fn(key), save_fn(key, name, filters)): see doc
        self.filter_key = None          # the table key for saved filters (str or callable)
        self.search_box = None          # an owner's search field (grid_search): kept in step
        self._fmeta = {}                # column -> (filter text, chip words) from the popover
        self._col_types = {}            # column -> colfilter.ColumnType the popover detected
        self._chip_order = []           # column indices / 'global', oldest change first
        self._hist, self._hist_i = [], -1   # views (filters + sort) for Back / Forward
        self._restoring = False
        self._popover = None
        self._distinct = collections.OrderedDict()     # checklist results of this source
        self._hl_words = []             # the search's words (lower case) highlighted in cells
        self._funnel_hover = None
        self._last_applied = ({}, "")
        self.on_open_row, self.on_open_blob = on_open_row, on_open_blob
        # where the rows are from, for the titles of windows opened from a cell ('urls,
        # History'): a string or a function returning one; set by the owner
        self.context = ""
        self.on_sort, self.on_filter, self.on_view_change = on_sort, on_filter, on_view_change
        self.on_selection = on_selection
        self.describe = describe
        self.row_style, self.on_context_menu = row_style, on_context_menu
        self.on_header_menu = on_header_menu
        self._formatters = {}           # column -> (formatter, header badge)
        self._formatter_colors = {}     # column -> text colour of its formatted values
        self.filter_delay = filter_delay
        self._frozen_n = 1 if frozen else 0

        self._fonts = grid_fonts(self)
        self._rh = self._fonts[0].metrics("linespace") + 8
        self._hh = self._fonts[2].metrics("linespace") + 10
        self._fh = self._fonts[3].metrics("linespace") + 10
        self._mincw = [max(1, f.measure(".")) for f in self._fonts]
        self._maxcw = [max(f.measure(ch) for ch in "WM@\u5b57") for f in self._fonts]
        self._avgcw = max(1, self._fonts[0].measure("0"))
        self._ellw = [f.measure("\u2026") for f in self._fonts]
        self._fit_cache = {}
        self._measure_cache = {}
        self._cw = CharWidths.of(self)
        self._charw, self._exactw = self._cw.tables, self._cw.exact
        self._cw.grids.add(self)

        st = ttk.Style(self)
        st.configure("GridFilter.TEntry", padding=(3, 0))
        st.configure("GridFilterBad.TEntry", padding=(3, 0), fieldbackground=C["rl"],
                     foreground=C["red"])
        st.configure("GridFilterOn.TEntry", padding=(3, 0), fieldbackground=C["hl"])

        # the search, Back / Forward and the chips of the filters in force, above the grid
        self._filter_bar_summary = ""
        self._runner = None             # (made below; the bar's buttons refer to the grid)
        self.filter_bar = colfilter.FilterBar(self, self, search=search)
        self._pane = ttk.PanedWindow(self, orient="horizontal")
        self._pane.pack(fill="both", expand=True)
        self._frame = tk.Frame(self._pane, bg=C["border"], bd=0)
        self._pane.add(self._frame, weight=1)
        opts = dict(bg=C["bg"], highlightthickness=0, bd=0, xscrollincrement=1,
                    yscrollincrement=1, confine=False, takefocus=0)
        head_h = self._hh + (self._fh if self._inline else 0)
        self._corner = tk.Canvas(self._frame, width=80, height=head_h, **opts)
        self._hdr = tk.Canvas(self._frame, height=head_h, **opts)
        self._fcv = tk.Canvas(self._frame, width=80, **opts)
        opts["takefocus"] = 1
        self._cv = tk.Canvas(self._frame, **opts)
        self._vsb = ttk.Scrollbar(self._frame, orient="vertical", command=self._on_vscroll)
        self._hsb = ttk.Scrollbar(self._frame, orient="horizontal", command=self._on_hscroll)
        self._corner.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        self._hdr.grid(row=0, column=1, sticky="ew", padx=(1, 0), pady=(1, 0))
        self._fcv.grid(row=1, column=0, sticky="ns", padx=(1, 0))
        self._cv.grid(row=1, column=1, sticky="nsew", padx=(1, 0))
        self._vsb.grid(row=1, column=2, sticky="ns")
        self._hsb.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._frame.columnconfigure(1, weight=1)
        self._frame.rowconfigure(1, weight=1)
        self._surf = dict((cv, _Surface(cv, self._fonts))
                          for cv in (self._corner, self._hdr, self._fcv, self._cv))
        self._layout_gen = 0
        self._base = 0

        self._build_inspector()
        self._runner = Runner(self, "grid-fetch", release=self._release_worker)
        # the filter popover's reads (values, counts) on their own worker: never behind rows
        self._frunner = Runner(self, "grid-filter", release=self._release_worker)
        self._empty = None              # the 'No rows match' panel (made when first needed)

        self._source = None
        self._cols = []
        self._widths = []
        self._user_sized = set()
        self._hidden = set()
        self._order = []
        self._col_order = []        # the user's display order of logical columns (frozen first)
        self._hdr_move = None       # a header drag-reorder in progress: [column, x0, moved]
        self._wrap = False           # wrap cell text over several lines
        self._wrap_lines = 3        # lines of a row while wrapping
        self._wrap_cache = {}
        self._pos = {}
        self._xs = [0]
        self._frozen = 0
        self._xoff = 0
        self._top = 0
        self._total = 0
        self._exact = False
        self._gen = 0
        self._cache = collections.OrderedDict()
        self._pending = set()
        self._failed = set()
        self._stored = 0                # windows stored so far (tells a redraw about new data)
        self.load_error = ""
        self._sized = False
        self._sort = (None, False)
        self._cur = None
        self._anchor = None
        self._sel = None
        self._fvars = {}
        self._entries = {}
        self._entry_shown = set()
        self._filter_errors = {}
        self._global = ""
        self._applied = None
        self._filter_after = None
        self._redraw_id = None
        self._drag = None
        self._hdr_press = None
        self._drawn = (0, 0, [])
        self._shown_rows = None         # (source, first, end, data) of the rows drawn last in full
        self._held = False              # the last redraw kept that frame (rows not read yet)
        self._target = (0, 0)           # rows the view shows once they are read
        self._last_top = 0
        self._direction = 1             # +1 scrolling down, -1 up (for reading ahead)
        self._drag_until = 0.0          # a scrollbar drag is going on until then
        self._drag_after = None
        self._first_after = None
        self._source_t0 = 0.0
        self._interrupted = set()      # (gen, window) reads stopped because nobody needs them
        self._win = limits.get("grid_window_rows")
        self.notice = ""                # a limit cut something off (e.g. a copy): said here
        self.frames = collections.Counter()     # 'drawn', 'held', 'empty' redraws (measuring)
        self._rows_cut = None               # rows the last redraw drew before its budget ran out
        self._cut_view = None               # the rows (first, end) that redraw was drawing
        self._view_key = None
        self._menu = None
        self._chooser = None
        self._marks = {}                # column index -> text drawn after its header name
        self.mark_tips = {}             # mark text -> what it means (the header tooltip)

        for cv in (self._cv, self._fcv, self._hdr, self._corner):
            cv.bind("<Configure>", lambda e: self._schedule())
            cv.bind("<Button-1>", lambda e, c=cv: self._on_press(e, c, False))
            cv.bind("<Shift-Button-1>", lambda e, c=cv: self._on_press(e, c, True))
            cv.bind("<Double-Button-1>", lambda e, c=cv: self._on_double(e, c))
            cv.bind("<B1-Motion>", lambda e, c=cv: self._on_drag(e, c))
            cv.bind("<ButtonRelease-1>", lambda e, c=cv: self._on_release(e, c))
            cv.bind("<Button-3>", lambda e, c=cv: self._on_right(e, c))
            self._bind_wheel(cv)
        for cv in (self._hdr, self._corner):
            cv.bind("<Motion>", lambda e, c=cv: self._on_motion(e, c))
            cv.bind("<Leave>", lambda e: self._set_funnel_hover(None), add="+")
            cv.bind("<Leave>", self._tip_hide, add="+")
        # the full text of a cut-off cell, shown when the mouse rests on it
        self._tip, self._tip_after, self._tip_cell, self._tip_view = None, None, None, None
        for cv in (self._cv, self._fcv):
            cv.bind("<Motion>", lambda e, c=cv: self._tip_motion(e, c))
            cv.bind("<Leave>", self._tip_hide, add="+")
            cv.bind("<ButtonPress>", self._tip_hide, add="+")
        # a tip never outlives its grid: switching tabs or closing hides it
        self.bind("<Unmap>", self._tip_hide, add="+")
        self.bind("<Destroy>", self._tip_hide, add="+")
        k = self._cv
        for seq, fn in (("<Up>", lambda e: self._key_move(-1, 0)),
                        ("<Down>", lambda e: self._key_move(1, 0)),
                        ("<Shift-Up>", lambda e: self._key_move(-1, 0, True)),
                        ("<Shift-Down>", lambda e: self._key_move(1, 0, True)),
                        ("<Left>", lambda e: self._key_move(0, -1)),
                        ("<Right>", lambda e: self._key_move(0, 1)),
                        ("<Prior>", lambda e: self._key_move(-self._page(), 0)),
                        ("<Next>", lambda e: self._key_move(self._page(), 0)),
                        ("<Shift-Prior>", lambda e: self._key_move(-self._page(), 0, True)),
                        ("<Shift-Next>", lambda e: self._key_move(self._page(), 0, True)),
                        ("<Home>", lambda e: self._key_jump(0, None)),
                        ("<End>", lambda e: self._key_jump(-1, None)),
                        ("<Control-Home>", lambda e: self._key_jump(0, None)),
                        ("<Control-End>", lambda e: self._key_jump(-1, None)),
                        ("<Control-Left>", lambda e: self._key_jump(None, 0)),
                        ("<Control-Right>", lambda e: self._key_jump(None, -1)),
                        ("<Return>", lambda e: self._open_current()),
                        ("<Shift-Return>", lambda e: self.view_value()),
                        ("<Control-c>", lambda e: self.copy_cell()),
                        ("<Control-C>", lambda e: self.copy_rows("tsv")),
                        ("<Control-a>", lambda e: self.select_all()),
                        ("<Control-z>", lambda e: self.undo_filter()),
                        ("<Control-Z>", lambda e: self.redo_filter()),     # Ctrl+Shift+Z
                        ("<Control-y>", lambda e: self.redo_filter()),
                        ("<Control-Y>", lambda e: self.redo_filter()),
                        ("<Alt-Down>", lambda e: self.open_filter_popover(
                            self._cur[1] if self._cur is not None else None))):
            k.bind(seq, lambda e, f=fn: (f(e), "break")[1])
        self._layout()                  # empty until set_source(): no frozen column either
        self._update_filter_bar()

    # -- public: source -------------------------------------------------------------------
    def set_source(self, source):
        """Show another source (None: empty). Resets sort, filters, widths and position."""
        self._runner.cancel()
        self.close_filter_popover()
        self._frunner.cancel()
        for e, wid, _tip in self._entries.values():
            self._hdr.delete(wid)
            e.destroy()
        self._entries, self._entry_shown, self._fvars = {}, set(), {}
        self._filter_errors = {}
        self._applied = None
        self._global = ""
        self._hl_words = []
        self._fmeta, self._col_types, self._chip_order = {}, {}, []
        self._distinct.clear()
        self._funnel_hover = None
        self._last_applied = ({}, "")
        for box in self._search_boxes():
            if box.var.get():
                box.var.set("")         # its filter went with the old source
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
            self._filter_after = None
        self._source = source
        self._cols = list(source.columns()) if source is not None else []
        self._frozen = min(self._frozen_n, len(self._cols))
        self._widths = [self._header_width(c) for c in range(len(self._cols))]
        if self._frozen:
            self._widths[0] = max(self._widths[0], 80)
        self._user_sized = set()
        self._hidden = set()
        self._col_order = []
        self._hdr_move = None
        self._marks = {}
        self._formatters = {}
        self._formatter_colors = {}
        self._sort = (None, False)
        self._cur = (0, self._frozen if len(self._cols) > self._frozen else 0) \
            if self._cols else None
        self._anchor, self._sel = None, None
        self._xoff = 0
        self._sized = False
        # the rows of the source shown until now stay in view (briefly, see _redraw) until
        # this one's first rows arrive; with no source there is nothing to wait for
        self._source_t0 = time.time()
        if source is None:
            self._shown_rows = None
        self._reset_rows()
        self._layout()
        if self._chooser is not None and self._chooser.winfo_exists():
            self._chooser.destroy()
        self._chooser = None
        self._hist, self._hist_i = [], -1
        if source is not None:
            self._push_history()
        self._update_filter_bar()

    @property
    def source(self):
        return self._source

    def refresh(self):
        """Drop the rows held and fetch them again (after the source changed its rows)."""
        self._distinct.clear()
        self._reset_rows(keep_top=True)

    def row_count_changed(self):
        """The source knows its row count now (or a new one): re-read it."""
        if self._source is None:
            return
        n = self._source.row_count()
        if n is not None:
            self._total, self._exact = n, True
            self._clamp()
            self._schedule()

    # -- public: view state (also used by tests) -------------------------------------------
    def columns(self):
        return list(self._cols)

    def row_count(self):
        """Rows the grid scrolls over now; row_count_exact() says whether that is final."""
        return self._total

    def row_count_exact(self):
        return self._exact

    def visible_row_range(self):
        """(first, end) of the rows drawn in the last redraw (end exclusive)."""
        return self._drawn[0], self._drawn[1]

    def target_row_range(self):
        """(first, end) of the rows the view is at: those drawn, or while waiting() those
        being read (shown as soon as they arrive)."""
        return self._target if self._held else (self._drawn[0], self._drawn[1])

    def waiting(self):
        """True while the rows drawn are kept in view because the rows at the view's new
        position are still being read."""
        return self._held

    def visible_columns(self):
        """Indices of the scrolling columns drawn in the last redraw, left to right."""
        return list(self._drawn[2])

    def displayed_columns(self):
        """Indices of the columns shown (frozen first, hidden ones left out)."""
        return list(range(self._frozen)) + list(self._order)

    def column_x(self, c):
        """Left edge of column c in the window of its canvas: the frozen column is always at
        0; the other columns move with the horizontal scroll (the cells canvas and the header
        canvas with the filter entries share that x)."""
        if c < self._frozen:
            return 0
        return self._xs[self._pos[c]] - self._xoff

    def column_width(self, c):
        return self._widths[c]

    def set_column_width(self, c, width):
        self._widths[c] = max(MIN_COL_W, int(width))
        self._user_sized.add(c)
        self._layout()

    def filter_entry(self, c):
        """The filter Entry of column c in the filter row (made on first use; it is on screen
        only while inline_filters() is on and the column is in view)."""
        if not 0 <= c < len(self._cols):
            return None
        return self._entry(c)[0]

    def filter_tip(self, c):
        """The tooltip of column c's filter: its syntax, or why the filter cannot be used."""
        if not 0 <= c < len(self._cols):
            return ""
        return self._entry(c)[2].text

    def filter_texts(self):
        return dict((self._cols[c], v.get()) for c, v in self._fvars.items() if v.get().strip())

    def filter_errors(self):
        """{column name: message} for filters that could not be used."""
        return dict((self._cols[c], m) for c, m in self._filter_errors.items())

    def current_cell(self):
        return self._cur

    def selected_rows(self):
        return self._sel

    def row_data(self, row):
        """(values, flags) of a row if it is loaded, else None."""
        w = self._cache.get(row // self._win)
        if w is None:
            return None
        i = row % self._win
        return w[i] if i < len(w) else None

    def visible_rows_data(self):
        """(values, flags) of the loaded rows in view (while waiting(), the rows kept)."""
        if self._held and self._shown_rows is not None:
            return [d for d in self._shown_rows[3] if d is not None]
        first, end = self._drawn[0], self._drawn[1]
        return [d for d in (self.row_data(r) for r in range(first, end)) if d is not None]

    def row_background(self, row):
        """Background colour row `row` is drawn with (None if it is not in view)."""
        ent = self._surf[self._cv].rowbg.items.get(row)
        return ent[1][0] if ent is not None else None

    def row_marker(self, row):
        """Colour of the marker drawn for row `row` by row_style (None: no marker, or the row
        is not in view)."""
        ent = self._surf[self._fcv].marker.items.get(row)
        return ent[1][0] if ent is not None else None

    def restyle(self):
        """Redraw soon, asking row_style again (e.g. after the rows' marks changed)."""
        self._schedule()

    def set_column_formatter(self, c, formatter, badge="", color=None):
        """Draw column c's values as formatter(value) returns them (None: the value itself);
        formatter None removes it. badge: a short word shown after the column name; color:
        the text colour of the formatted values (default green)."""
        if not 0 <= c < len(self._cols):
            return
        if formatter is None:
            self._formatters.pop(c, None)
            self._formatter_colors.pop(c, None)
        else:
            self._formatters[c] = (formatter, badge or "")
            self._formatter_colors[c] = color or C["green"]
        self._fit_cache.clear()
        self._insp_key = None
        if c not in self._user_sized:
            want = self._header_width(c)
            for values, _flags in self.visible_rows_data()[:40]:
                if c < len(values):
                    text = self._formatted(values[c], c)
                    if text is not None:
                        want = max(want, self._text_width(text, 0) + 2 * PAD + 2)
            self._widths[c] = max(self._widths[c], min(want, MAX_AUTOSIZE_W))
        self._layout()

    def column_formatter(self, c):
        """(formatter, badge) of column c, or None."""
        return self._formatters.get(c)

    def display_text(self, v, c):
        """The text column c draws for value v (formatted, or the value itself)."""
        text = self._formatted(v, c)
        return text if text is not None else cell_text(v)[0]

    def _formatted(self, v, c):
        ent = self._formatters.get(c)
        if ent is None or v is None or v is _MISSING or isinstance(v, Locator):
            return None
        try:
            text = ent[0](v)
        except Exception:       # noqa: BLE001 - a value the formatter cannot read shows raw
            return None
        return text if isinstance(text, str) else None

    def bind_key(self, sequence, fn):
        """Bind a key on the grid (it has the keyboard focus after a click): fn() is called
        and the key goes no further."""
        self._cv.bind(sequence, lambda e: (fn(), "break")[1])

    def view_state(self):
        """What the user changed in the view, by column name (JSON-safe): the widths of the
        columns they sized, the hidden columns, the column order, wrapping and the sort.
        apply_view_state() restores it."""
        c, desc = self._sort
        return {"widths": dict((self._cols[i], int(self._widths[i]))
                               for i in sorted(self._user_sized) if i < len(self._cols)),
                "hidden": [self._cols[i] for i in sorted(self._hidden) if i < len(self._cols)],
                "order": [self._cols[i] for i in self._col_order if i < len(self._cols)],
                "wrap": bool(self._wrap),
                "sort": [self._cols[c], bool(desc)] if c is not None else None}

    def apply_view_state(self, state):
        """Restore a view_state() on the current source; columns it names that the source no
        longer has are skipped."""
        if not state or not self._cols:
            return
        index = {}
        for i, name in enumerate(self._cols):
            index.setdefault(name, i)
        for name, width in (state.get("widths") or {}).items():
            c = index.get(name)
            if c is not None and isinstance(width, (int, float)):
                self._widths[c] = max(MIN_COL_W, int(width))
                self._user_sized.add(c)
        for name in state.get("hidden") or ():
            c = index.get(name)
            if c is not None and c >= self._frozen:
                self._hidden.add(c)
        order = state.get("order") or ()
        if order:
            seq, seen = [], set()
            for name in order:
                c = index.get(name)
                if c is not None and c not in seen and c >= self._frozen:
                    seen.add(c)
                    seq.append(c)
            if seq:
                self._col_order = seq
        if state.get("wrap") and not self._wrap:
            self._wrap = True
            self._rh = self._fonts[0].metrics("linespace") * self._wrap_lines + 8
        elif not state.get("wrap") and self._wrap:
            self._wrap = False
            self._rh = self._fonts[0].metrics("linespace") + 8
        self._layout()
        if self._cur is not None and self._cur[1] in self._hidden:
            shown = self.displayed_columns()
            later = [x for x in shown if x > self._cur[1]]
            self._cur = (self._cur[0], later[0] if later else shown[-1])
        sort = state.get("sort")
        if sort and index.get(sort[0]) is not None:
            self.sort_by(index[sort[0]], bool(sort[1]))

    def loading(self):
        """True while windows are being fetched."""
        return bool(self._pending) or self._runner.busy()

    def text_items(self, canvas="cells"):
        """(item, text) of the text items shown now on one canvas: 'cells', 'header',
        'frozen' or 'corner'. Items kept hidden for reuse are not counted."""
        cv = {"cells": self._cv, "header": self._hdr, "frozen": self._fcv,
              "corner": self._corner}[canvas]
        surf = self._surf[cv]
        out = []
        for keyed in (surf.cell, surf.hdrtext):
            out.extend((ent[0], ent[1][0]) for ent in keyed.items.values())
        return out

    def badge_items(self):
        """{(row, column): text} of the size notes drawn on long or multi-line text cells."""
        return dict((k, ent[1][0]) for k, ent in self._surf[self._cv].badge.items.items())

    def hidden_columns(self):
        return set(self._hidden)

    def set_header_marks(self, marks):
        """{column index: text} drawn after those column names (e.g. a link glyph); the
        marks are cleared when the source changes."""
        marks = dict(marks)
        if marks != self._marks:
            self._marks = marks
            self._view_key = None
            self._schedule()

    def header_marks(self):
        return dict(self._marks)

    def sort_state(self):
        c, desc = self._sort
        return (self._cols[c] if c is not None else None), desc

    # -- public: scrolling ------------------------------------------------------------------
    def scroll_rows(self, n):
        self._top += int(n)
        self._clamp()
        self._schedule()

    def scroll_to_row(self, row):
        self._top = int(row)
        self._clamp()
        self._schedule()

    def scroll_x(self, pixels):
        self._xoff += int(pixels)
        self._clamp()
        self._schedule()

    def xview_moveto(self, fraction):
        self._xoff = int(float(fraction) * self._xs[-1])
        self._clamp()
        self._schedule()

    def yview_moveto(self, fraction):
        self._top = int(float(fraction) * self._total)
        self._clamp()
        self._schedule()

    def redraw_now(self):
        """Redraw at once instead of when idle."""
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
        self._redraw(budget=None)           # every row, at once

    # -- public: selection and copy ---------------------------------------------------------
    def set_current_cell(self, row, col, extend=False):
        if not self._cols or self._total <= 0:
            return
        row = max(0, min(int(row), self._total - 1))
        if col is None or (col not in self._pos and col >= self._frozen):
            col = self._cur[1] if self._cur else self._frozen
        self._cur = (row, col)
        if extend and self._anchor is not None:
            self._sel = (min(self._anchor, row), max(self._anchor, row))
        else:
            self._anchor, self._sel = row, (row, row)
        self._ensure_visible(row, col)
        self._schedule()
        if self.on_selection is not None:
            self.on_selection(self._sel)

    def select_all(self):
        if self._total > 0:
            self._anchor, self._sel = 0, (0, self._total - 1)
            self._schedule()
            if self.on_selection is not None:
                self.on_selection(self._sel)

    def copy_cell(self):
        text = self.cell_copy_text()
        if text is not None:
            self.clipboard_clear()
            self.clipboard_append(text)

    def cell_copy_text(self, raw=False):
        """Text of the current cell: as drawn (a formatted column's formatted text), or with
        raw=True the stored value."""
        if self._cur is None:
            return None
        got = self._fetch_sync(self._cur[0], self._cur[0])
        if not got:
            return None
        values = got[0][0]
        v = values[self._cur[1]] if self._cur[1] < len(values) else None
        shown = None if raw else self._formatted(v, self._cur[1])
        return shown if shown is not None else plain_text(v)

    def copy_raw_cell(self):
        text = self.cell_copy_text(raw=True)
        if text is not None:
            self.clipboard_clear()
            self.clipboard_append(text)

    def rows_copy_text(self, fmt="tsv"):
        """Selected rows (or the current row) in TSV / CSV / JSON with a header, hidden
        columns left out. At most limits 'grid_copy_rows' rows (notice says when cut)."""
        if self._sel is not None:
            lo, hi = self._sel
        elif self._cur is not None:
            lo = hi = self._cur[0]
        else:
            return None
        cap = limits.get("grid_copy_rows")
        if hi - lo + 1 > cap:
            hi = lo + cap - 1
            self.notice = ("copied the first %s of the selected rows (raise %s)"
                           % (format(cap, ","), limits.hint("grid_copy_rows")))
            self._view_key = None
            self._schedule()
        else:
            self.notice = ""
        rows = self._fetch_sync(lo, hi)
        cols = self.displayed_columns()
        names = [self._cols[c] for c in cols]
        if fmt == "json":
            out = []
            for values, _flags in rows:
                out.append(dict((n, json_value(values[c] if c < len(values) else None))
                                for n, c in zip(names, cols)))
            return json.dumps(out, indent=2, ensure_ascii=False)
        if fmt == "csv":
            buf = io.StringIO()
            # NUL as \x00 in every cell; text (not numbers) spreadsheet-safe: engine.csvcells
            w = csv_writer(buf, formulas=False, lineterminator="\n")
            w.writerow([csv_text(n) for n in names])

            def cell(v):
                text = plain_text(v)
                return text if isinstance(v, (int, float)) else csv_text(text)
            for values, _flags in rows:
                w.writerow([cell(values[c]) if c < len(values) else "" for c in cols])
            return buf.getvalue()

        def esc(s):
            return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n") \
                    .replace("\r", "\\r")

        def tsv_cell(v):
            # TSV has no quoting, so a cell pasted into a spreadsheet would run as a
            # formula when it starts with = + - @ (engine.csvcells); numbers are exempt.
            text = v if isinstance(v, str) else plain_text(v)
            if not isinstance(v, (int, float)):
                text = formula_safe(text)
            return esc(text)
        lines = ["\t".join(tsv_cell(n) for n in names)]
        for values, _flags in rows:
            lines.append("\t".join(tsv_cell(values[c]) if c < len(values) else ""
                                   for c in cols))
        return "\n".join(lines) + "\n"

    def copy_rows(self, fmt="tsv"):
        text = self.rows_copy_text(fmt)
        if text is not None:
            self.clipboard_clear()
            self.clipboard_append(text)

    def fetch_rows(self, lo, hi):
        """(values, flags) of rows lo..hi (inclusive), read now if they are not loaded."""
        return self._fetch_sync(lo, hi)

    # -- public: filters --------------------------------------------------------------------
    def set_filter_text(self, c, text, apply=True):
        """Put an expression in column c's filter (e.g. 'Filter to this value')."""
        if not 0 <= c < len(self._cols):
            return
        self._fvar(c).set(text)
        if apply:
            self.apply_filters()

    def set_global_filter(self, text, apply=False):
        """The search of all columns: rows where every word occurs in some column."""
        self._global = (text or "").strip()
        if apply:
            self.apply_filters()
        else:
            self._debounce_filters()

    def global_filter(self):
        return self._global

    def clear_filters(self):
        for v in self._fvars.values():
            v.set("")
        self._global = ""
        self._sync_search_boxes()
        self.apply_filters()

    def apply_filters(self):
        """Validate every filter, mark the ones that cannot be used, and hand the valid
        expressions and the global text to the source (only when they changed)."""
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
            self._filter_after = None
        exprs, bad, by_col = {}, {}, {}
        for c, var in self._fvars.items():
            text = var.get().strip()
            if not text or c >= len(self._cols):
                continue
            msg = check_expr(text)
            if msg:
                bad[c] = msg
            else:
                exprs[self._cols[c]] = text
                by_col[c] = text
        self._filter_errors = bad
        for c in self._entries:
            self._style_entry(c)
        key = (tuple(sorted(exprs.items())), self._global)
        if key == self._applied or self._source is None:
            self._update_filter_bar()
            return
        if hasattr(self._source, "set_filters"):
            try:
                self._source.set_filters(exprs, self._global)
            except FilterError as e:
                self.load_error = str(e)
                self._update_filter_bar()
                self._schedule()
                return
        self._applied = key
        self._note_changes(by_col)
        try:
            self._hl_words = [w for w in (ascii_lower(t) for t in parse_words(self._global)) if w]
        except FilterError:
            self._hl_words = []
        self._reset_rows()
        self._push_history()
        self._update_filter_bar()
        if self.on_filter is not None:
            self.on_filter(exprs, self._global)
        try:
            self.event_generate("<<GridFiltersChanged>>", when="tail")
        except tk.TclError:
            pass

    def active_filters(self):
        """The filters in force: column names (valid filters only), and 'all columns' for the
        filter of all columns."""
        out = [self._cols[c] for c, var in sorted(self._fvars.items())
               if c < len(self._cols) and var.get().strip() and c not in self._filter_errors]
        if self._global:
            out.append("all columns")
        return out

    def _note_changes(self, by_col):
        """Keep the order in which the filters in force were last changed (the chips, and the
        condition the empty state offers to remove)."""
        prev, prev_global = self._last_applied
        now = dict(by_col)
        order = [k for k in self._chip_order if (k == "global" and self._global) or
                 (k != "global" and k in now)]
        for c, text in sorted(now.items()):
            if prev.get(c) != text:
                if c in order:
                    order.remove(c)
                order.append(c)
        if self._global and self._global != prev_global:
            if "global" in order:
                order.remove("global")
            order.append("global")
        self._chip_order = order
        for c in list(self._fmeta):
            if self._fmeta[c][0] != now.get(c):
                del self._fmeta[c]
        self._last_applied = (now, self._global)

    def _update_filter_bar(self, *_ignored):
        names = self.active_filters()
        bad = self._filter_errors
        text = ""
        if names:
            text = "%d filter%s active: %s" % (len(names), "" if len(names) == 1 else "s",
                                               ", ".join(names))
        if bad:
            text += ("; " if text else "") + "%d cannot be used (hover it for why)" % len(bad)
        self._filter_bar_summary = text
        bar = self.filter_bar
        bar.update_bar()
        own_search = bar.search is not None and self.search_box is None
        show = own_search or bool(names or bad) or self.can_undo_filter() or \
            self.can_redo_filter()
        if show and not bar.winfo_manager():
            bar.pack(fill="x", before=self._pane)
        elif not show and bar.winfo_manager():
            bar.pack_forget()
        self._update_empty_state()

    def filter_bar_text(self):
        """What the filters in force are, in one line ('2 filters active: col3, all
        columns'); '' while no filter is in force (the chips then are hidden)."""
        return self._filter_bar_summary if self.filter_bar.chips.winfo_manager() else ""

    def filter_chips(self):
        """[(key, words, tooltip, bad)] of the chips: one per column filter (key: the column
        index) and one for the search of all columns (key 'global'), oldest change first."""
        active = dict((c, var.get().strip()) for c, var in self._fvars.items()
                      if c < len(self._cols) and var.get().strip())
        keys = [k for k in self._chip_order if k != "global" and k in active]
        keys += [c for c in sorted(active) if c not in keys]
        if self._global:
            at = self._chip_order.index("global") if "global" in self._chip_order else len(keys)
            keys.insert(min(at, len(keys)), "global")
        out = []
        for k in keys:
            if k == "global":
                words = colfilter.describe_filter(None, None, words=self._global)
                out.append((k, words, "Search of all columns: %s\nClick to edit; ×, "
                            "Delete or Backspace removes it" % self._global, False))
                continue
            text = active[k]
            msg = self._filter_errors.get(k)
            words = self.filter_description(k)
            tip = "%s\nfilter: %s" % (words, text)
            if msg:
                tip += "\nCannot use this filter: " + msg
            tip += "\nClick to edit; ×, Delete or Backspace removes it"
            out.append((k, words, tip, bool(msg)))
        return out

    def filter_description(self, c):
        """The chip words of column c's filter ('status = 3')."""
        var = self._fvars.get(c)
        text = var.get().strip() if var is not None else ""
        if not text or c >= len(self._cols):
            return ""
        name = self._cols[c]
        meta = self._fmeta.get(c)
        if meta is not None and meta[0] == text and meta[1]:
            return meta[1]
        if c in self._filter_errors:
            return "%s: %s" % (name, text)
        try:
            return colfilter.describe_filter(name, text, self.column_type(c))
        except Exception:               # noqa: BLE001 - words are a courtesy
            return "%s: %s" % (name, text)

    def column_type(self, c):
        """The colfilter.ColumnType known for column c (the popover detected it, or the column
        is shown as dates), else None."""
        ct = self._col_types.get(c)
        if ct is not None:
            return ct
        kind = self._formatter_kind(c)
        return colfilter.ColumnType("date", date_kind=kind) if kind else None

    def note_column_type(self, c, ct):
        self._col_types[c] = ct

    def _formatter_kind(self, c):
        """The engine.timeline kind column c is shown as (Show as date), else None."""
        ent = self._formatters.get(c)
        if ent is None:
            return None
        badge = ent[1]
        for k in tl.KINDS:
            if badge in ("UTC " + tl.SHORT[k], tl.SHORT[k]):
                return k
        return getattr(ent[0], "date_kind", None)

    def edit_filter(self, key):
        """A chip was clicked: open its column's popover (the search: focus its box)."""
        if key == "global":
            for box in self._search_boxes():
                try:
                    box.entry.focus_set()
                    return None
                except (tk.TclError, AttributeError):
                    pass
            return None
        return self.open_filter_popover(key)

    def remove_filter(self, key):
        """Remove one filter (a chip's ×): a column index or 'global'."""
        if key == "global":
            self._global = ""
            self._sync_search_boxes()
            self.apply_filters()
        elif isinstance(key, int) and key in self._fvars:
            self.set_filter_text(key, "")

    def last_filter(self):
        """The key of the filter changed last ('global', a column index), or None."""
        chips = self.filter_chips()
        if not chips:
            return None
        order = [k for k in self._chip_order if any(k == ch[0] for ch in chips)]
        return order[-1] if order else chips[-1][0]

    def remove_last_filter(self):
        k = self.last_filter()
        if k is not None:
            self.remove_filter(k)

    def filter_count_text(self):
        """'1,204 of 2,460,000 rows' while a filter is in force ('' when none is)."""
        if not self.active_filters() or self._source is None:
            return ""
        total = getattr(self._source, "total", None)
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        of = (" of %s rows" % colfilter.fmt_int(total)) if total is not None else " rows"
        if not self._exact:
            return "counting…" + of
        return colfilter.fmt_int(self._total) + of

    # -- filters: history, saved sets, copies ------------------------------------------------
    def _view_now(self):
        filters = dict((self._cols[c], var.get().strip()) for c, var in self._fvars.items()
                       if c < len(self._cols) and var.get().strip())
        sc, sd = self._sort
        natural = sc is None or (sc < self._frozen and not sd)
        return {"columns": filters, "search": self._global,
                "sort": None if natural else [self._cols[sc], bool(sd)]}

    def _push_history(self):
        if self._restoring or self._source is None:
            return
        view = self._view_now()
        if 0 <= self._hist_i < len(self._hist) and self._hist[self._hist_i] == view:
            return
        del self._hist[self._hist_i + 1:]
        self._hist.append(view)
        self._hist_i = len(self._hist) - 1

    def filter_history(self):
        """(views, index of the one shown): each view {'columns', 'search', 'sort'}."""
        return [dict(v) for v in self._hist], self._hist_i

    def can_undo_filter(self):
        return self._hist_i > 0

    def can_redo_filter(self):
        return 0 <= self._hist_i < len(self._hist) - 1

    def undo_filter(self):
        """Back to the filters and sort shown before (Ctrl+Z); False when there are none."""
        if not self.can_undo_filter():
            return False
        self._hist_i -= 1
        self._restore_view(self._hist[self._hist_i])
        return True

    def redo_filter(self):
        """Forward again (Ctrl+Y); False at the newest view."""
        if not self.can_redo_filter():
            return False
        self._hist_i += 1
        self._restore_view(self._hist[self._hist_i])
        return True

    def _restore_view(self, view):
        self._restoring = True
        try:
            skipped = self._apply_view(view)
        finally:
            self._restoring = False
        self._update_filter_bar()
        return skipped

    def _apply_view(self, view):
        """Show a view {'columns', 'search', 'sort'}; returns the column names it names that
        this source does not have."""
        index = {}
        for i, name in enumerate(self._cols):
            index.setdefault(name, i)
        cols = view.get("columns") or {}
        want = dict((index[n], t) for n, t in cols.items() if n in index and index[n] >= 0)
        for c, var in list(self._fvars.items()):
            if c not in want and var.get():
                var.set("")
        for c, t in want.items():
            self._fvar(c).set(t)
        self._global = (view.get("search") or "").strip()
        self._sync_search_boxes()
        sort = view.get("sort")
        sc, sd = self._sort
        if sort and sort[0] in index:
            if (sc, bool(sd)) != (index[sort[0]], bool(sort[1])):
                self.sort_by(index[sort[0]], bool(sort[1]))
        elif not sort and sc is not None and not (sc < self._frozen and not sd):
            if self._frozen:
                self.sort_by(0, False)
            else:
                if hasattr(self._source, "sort"):
                    self._source.sort(None, False)
                self._sort = (None, False)
                self._reset_rows()
        self.apply_filters()
        return [n for n in cols if n not in index]

    def _filter_key(self):
        k = self.filter_key
        if callable(k):
            try:
                k = k()
            except Exception:           # noqa: BLE001 - no key: nothing saved
                k = None
        if k is None:
            k = getattr(self._source, "table", None)
        return k if isinstance(k, str) and k else None

    def saved_filter_sets(self):
        """{name: filters} saved for this table ({} without the saved_filters hook)."""
        hook, key = self.saved_filters, self._filter_key()
        if not hook or key is None:
            return {}
        try:
            got = hook[0](key)
        except Exception:               # noqa: BLE001 - a store that fails lists nothing
            return {}
        if not isinstance(got, dict):
            return {}
        return dict((str(n), f) for n, f in got.items() if isinstance(f, dict))

    def current_filters(self):
        """The filters and sort in force as a saved filter holds them (JSON-safe)."""
        return self._view_now()

    def save_current_filter(self, name):
        """Save the filters in force under a name (the saved_filters hook); True when saved."""
        name = (name or "").strip()
        hook, key = self.saved_filters, self._filter_key()
        if not name or not hook or key is None:
            return False
        try:
            hook[1](key, name, self._view_now())
        except Exception as e:          # noqa: BLE001 - said, never raised
            self.notice = "could not save the filter %r: %s" % (name, e)
            self._view_key = None
            self._schedule()
            return False
        self.filter_bar.update_bar()
        return True

    def delete_saved_filter(self, name):
        hook, key = self.saved_filters, self._filter_key()
        if not hook or key is None:
            return False
        try:
            hook[1](key, name, None)
        except Exception:               # noqa: BLE001
            return False
        return True

    def apply_saved_filter(self, filters):
        """Apply saved filters; returns the names of the columns this table lacks (skipped,
        and said in notice)."""
        if not isinstance(filters, dict):
            return []
        cols = filters.get("columns")
        view = {"columns": dict((str(k), str(v)) for k, v in cols.items()
                                if isinstance(v, str) and v.strip())
                if isinstance(cols, dict) else {},
                "search": filters.get("search") if isinstance(filters.get("search"), str)
                else "",
                "sort": filters.get("sort") if isinstance(filters.get("sort"), (list, tuple))
                and len(filters.get("sort")) == 2 else None}
        self._restoring = True
        try:
            skipped = self._apply_view(view)
        finally:
            self._restoring = False
        self._push_history()
        self._update_filter_bar()
        if skipped:
            self.notice = "saved filter: no column %s in this table (skipped)" % \
                ", ".join(sorted(skipped))
            self._view_key = None
            self._schedule()
        return skipped

    def save_filter_dialog(self):
        from tkinter import simpledialog
        try:
            name = simpledialog.askstring("Save filter", "A name for the filters in force:",
                                          parent=self.winfo_toplevel())
        except tk.TclError:
            return False
        return self.save_current_filter(name) if name else False

    def _valid_exprs(self):
        return [(self._cols[c], var.get().strip()) for c, var in sorted(self._fvars.items())
                if c < len(self._cols) and var.get().strip() and c not in self._filter_errors]

    def sql_where(self):
        """The filters in force as an SQL WHERE clause with the values written in."""
        cols = [n for i, n in enumerate(self._cols)
                if not (i < self._frozen and n == colfilter.RID)]
        enc = getattr(self._source, "encoding", "utf-8") or "utf-8"
        return colfilter.sql_where_text(dict(self._valid_exprs()), self._global, cols,
                                        enc if isinstance(enc, str) else "utf-8")

    def filter_text(self):
        """The filters in force as 'column: expression' lines (and 'search: words')."""
        return colfilter.filter_lines(self._valid_exprs(), self._global)

    def copy_text(self, text):
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    # -- filters: search boxes, the filter row, the popover ------------------------------------
    def use_search_box(self, box):
        """An owner's search field drives the search of all columns (grid_search): the bar's
        own box is hidden and the owner's is kept in step (cleared, Back / Forward)."""
        self.search_box = box
        own = self.filter_bar.search
        if own is not None and own.winfo_manager():
            own.pack_forget()
        self._update_filter_bar()

    def _search_boxes(self):
        out = []
        own = getattr(getattr(self, "filter_bar", None), "search", None)
        if own is not None and self.search_box is None:
            out.append(own)
        if self.search_box is not None:
            out.append(self.search_box)
        return out

    def _sync_search_boxes(self):
        for box in self._search_boxes():
            try:
                if box.var.get().strip() != self._global:
                    box.var.set(self._global)
            except (tk.TclError, AttributeError):
                pass

    def inline_filters(self):
        """True while the filter row under the headers is shown."""
        return self._inline

    def set_inline_filters(self, on):
        """Show or hide the filter row (one expression field per column under its header)."""
        on = bool(on)
        if on == self._inline:
            return
        self._inline = on
        h = self._hh + (self._fh if on else 0)
        for cv in (self._hdr, self._corner):
            cv.configure(height=h)
        if not on:
            for c in self._entry_shown:
                self._hdr.itemconfigure(self._entries[c][1], state="hidden")
            self._entry_shown = set()
        self._layout_gen += 1
        self._schedule()
        self._update_filter_bar()

    def filterable(self, c):
        """True for a column that has a filter (not the frozen row locator)."""
        return c is not None and self._frozen <= c < len(self._cols)

    def open_filter_popover(self, c):
        """Open column c's filter popover (the header funnel); returns it, or None."""
        if not self.filterable(c) or self._source is None:
            return None
        self.close_filter_popover()
        try:
            pop = self._popover
            if pop is None or not pop.winfo_exists():
                pop = self._popover = colfilter.FilterPopover(self)     # built once, reused
            return pop.show(c)
        except Exception as e:          # noqa: BLE001 - said, never raised to the user
            try:
                if self._popover is not None:
                    self._popover.close()
            except Exception:           # noqa: BLE001
                pass
            self.notice = "could not open the filter of %s: %s" % (self._cols[c], e)
            self._view_key = None
            self._schedule()
            return None

    def filter_popover(self):
        """The filter popover while it is shown, else None."""
        pop = self._popover
        return pop if pop is not None and pop.alive else None

    def close_filter_popover(self):
        pop = self._popover
        if pop is not None and pop.alive:
            pop.close()

    def popover_closed(self, pop):
        """The popover was closed (it stays built for the next column)."""

    def filter_runner(self):
        return self._frunner

    def distinct_cache(self, key, value=_MISSING):
        """A checklist result of this source by (column, other filters, limits): get it, or
        with a value keep it."""
        if value is _MISSING:
            return self._distinct.get(key)
        self._distinct[key] = value
        return value

    def filter_context(self, c):
        """What column c's popover starts from (read now, on the Tk thread: no data read)."""
        src = self._source
        name = self._cols[c]
        decl = ""
        fn = getattr(src, "column_types", None)
        if fn is not None:
            try:
                decl = str((fn() or {}).get(name) or "")
            except Exception:           # noqa: BLE001 - no declared type then
                decl = ""
        want = max(40, 2 * limits.get("timeline_sample_rows"))
        sample = []
        for rows in list(self._cache.values()):
            for values, _flags in rows:
                if c < len(values):
                    sample.append(values[c])
            if len(sample) >= want:
                break
        others = dict((self._cols[k], var.get().strip()) for k, var in self._fvars.items()
                      if k != c and k < len(self._cols) and var.get().strip()
                      and k not in self._filter_errors)
        try:
            flt = Filter(col_exprs=others, words=parse_words(self._global))
            flt = flt if flt else None
        except FilterError:
            flt = None
        total = getattr(src, "total", None)
        enc = getattr(src, "encoding", "utf-8")

        def display(v):
            if isinstance(v, (colfilter.LargeBlob, bytes, bytearray)) and                     not isinstance(v, InvalidText):
                return colfilter.short_value(v, 40)
            return self.display_text(v, c)
        var = self._fvars.get(c)
        return {"column": name, "source": src, "decl": decl, "sample": sample[:want],
                "date_hint": self._formatter_kind(c), "others": flt,
                "others_count": len(others) + (1 if self._global else 0),
                "total": total if isinstance(total, int) and not isinstance(total, bool)
                else None,
                "encoding": enc if isinstance(enc, str) else "utf-8", "display": display,
                "text": var.get().strip() if var is not None else ""}

    def filter_with(self, c, text):
        """A Filter of the other filters in force and `text` for column c (None: no filter)."""
        exprs = dict((self._cols[k], var.get().strip()) for k, var in self._fvars.items()
                     if k != c and k < len(self._cols) and var.get().strip()
                     and k not in self._filter_errors)
        if text:
            exprs[self._cols[c]] = text
        try:
            flt = Filter(col_exprs=exprs, words=parse_words(self._global))
        except FilterError:
            return None
        return flt if flt else None

    def apply_column_filter(self, c, text, description=None):
        """Set column c's filter (the popover's Apply); description: the chip's words."""
        if not 0 <= c < len(self._cols):
            return
        text = (text or "").strip()
        if text and description:
            self._fmeta[c] = (text, description)
        else:
            self._fmeta.pop(c, None)
        self.set_filter_text(c, text)

    def header_cell_root(self, c):
        """Screen position just under column c's header (where its popover opens)."""
        if c < self._frozen:
            cv, x = self._corner, 0
        else:
            cv, x = self._hdr, max(0, self.column_x(c))
        return cv.winfo_rootx() + x, cv.winfo_rooty() + self._hh

    def quick_filter(self, c, how, value=None, values=None):
        """The cell menu's quick filters on column c: how is 'only' (this value), 'exclude',
        'day' / 'hour' (of a date column's value) or 'values' (IN the values given). Returns
        the filter text set, or None when the value cannot be written as a filter."""
        if not self.filterable(c):
            return None
        var = self._fvars.get(c)
        current = var.get().strip() if var is not None else ""
        try:
            if how == "only":
                text = value_expr(value)
            elif how == "exclude":
                if isinstance(value, InvalidText) or (isinstance(value, bytes) and
                                                      len(value) > colfilter.BLOB_VALUE_MAX):
                    return None
                text = condition_text("not_in", [value])
                e = None
                if current and c not in self._filter_errors:
                    e = colfilter.parse_expr(current)
                if e is not None and e.kind == "notin":
                    vals = [x for _k, x in e.items] + ([None] if e.has_null else []) + [value]
                    text = condition_text("not_in", vals)
                elif e is not None:
                    text = colfilter.combine(current, "AND", text)
            elif how in ("day", "hour"):
                ct = self.column_type(c)
                if ct is None or ct.kind != "date":
                    return None
                dt = tl.to_utc(value, ct.date_kind)
                if dt is None:
                    return None
                text = colfilter.date_condition(ct, "on" if how == "day" else "hour", (dt,))
            elif how == "values":
                vals = []
                seen = set()
                for v in values or ():
                    k = colfilter._vkey(v)
                    if k not in seen:
                        seen.add(k)
                        vals.append(v)
                if not vals or not all(colfilter.expressible(v) for v in vals):
                    return None
                text = condition_text("in", vals) if len(vals) > 1 else value_expr(vals[0])
            else:
                return None
        except (ValueError, TypeError, FilterError):
            return None
        if not text:
            return None
        self._fmeta.pop(c, None)
        self.set_filter_text(c, text)
        return text

    # -- the empty state -------------------------------------------------------------------------
    def _no_match(self):
        return (self._source is not None and self._exact and self._total == 0
                and not self.load_error and bool(self.active_filters()))

    def empty_state(self):
        """The text of the 'No rows match' panel ('' while it is not shown)."""
        e = self._empty
        if e is None or not e.winfo_manager():
            return ""
        return str(e.msg.cget("text"))

    def _update_empty_state(self):
        if not self._no_match():
            if self._empty is not None and self._empty.winfo_manager():
                self._empty.place_forget()
            return
        if self._empty is None:
            e = self._empty = tk.Frame(self._frame, background=K["card"], highlightthickness=1,
                                       highlightbackground=K["border"])
            from appicons import illustration
            e.art = illustration(self, "empty", 180)    # kept: Tk drops unreferenced images
            if e.art is not None:
                tk.Label(e, image=e.art, background=K["card"]).pack(padx=16, pady=(12, 0))
            e.msg = tk.Label(e, text="", background=K["card"], foreground=K["heading"],
                             font=F["heading"], wraplength=440, justify="center")
            e.msg.pack(padx=16, pady=(12, 8))
            row = tk.Frame(e, background=K["card"])
            row.pack(pady=(0, 12))
            e.remove_btn = ttk.Button(row, text="", style="Primary.TButton",
                                      command=self.remove_last_filter)
            e.remove_btn.pack(side="left", padx=4)
            e.clear_btn = ttk.Button(row, text="Clear all filters", command=self.clear_filters)
            e.clear_btn.pack(side="left", padx=4)
        e = self._empty
        k = self.last_filter()
        words = ""
        for key, text, _tip, _bad in self.filter_chips():
            if key == k:
                words = text
        e.msg.configure(text="No rows match — remove “%s”?" % colfilter.cut(words, 60)
                        if words else "No rows match the filters")
        e.remove_btn.configure(text="Remove “%s”" % colfilter.cut(words, 32))
        if not e.winfo_manager():
            e.place(in_=self._cv, relx=0.5, rely=0.25, anchor="n")
        e.lift()

    # -- public: columns ----------------------------------------------------------------------
    def hide_column(self, c):
        if c < self._frozen or c in self._hidden or c >= len(self._cols):
            return
        var = self._fvars.get(c)
        if var is not None and var.get().strip():
            var.set("")
            self.apply_filters()     # a hidden column keeps no invisible filter
        self._hidden.add(c)
        self._layout()
        if self._cur is not None and self._cur[1] == c:
            shown = self.displayed_columns()
            nxt = [x for x in shown if x > c]
            self._cur = (self._cur[0], nxt[0] if nxt else shown[-1])

    def show_column(self, c):
        if c in self._hidden:
            self._hidden.discard(c)
            self._layout()

    # -- column order -------------------------------------------------------------------------
    def move_column(self, c, delta):
        """Shift column c one step left (delta=-1) or right (delta=+1) in the display
        order; False when there is nowhere to go (the frozen column never moves)."""
        if c is None or c < self._frozen or c not in self._col_order:
            return False
        i = self._col_order.index(c)
        j = max(0, min(len(self._col_order) - 1, i + delta))
        if i == j:
            return False
        self._col_order.insert(j, self._col_order.pop(i))
        self._layout()
        return True

    def _move_to_vpos(self, c, vpos):
        """Move logical column c to visual slot vpos (0-based among the shown scrolling
        columns); True when the order changed."""
        vis = [x for x in self._order if x != c]
        vpos = max(0, min(len(vis), vpos))
        order = [x for x in self._col_order if x != c]
        if vpos >= len(vis):
            order.append(c)
        else:
            order.insert(order.index(vis[vpos]), c)
        if order == self._col_order:
            return False
        self._col_order = order
        self._layout()
        return True

    def _drop_vpos(self, x):
        """The visual slot a header drag at canvas x would drop the column into: the
        first column whose middle is right of the pointer."""
        sx = x + self._xoff
        xs, order = self._xs, self._order
        for i in range(len(order)):
            if sx < (xs[i] + xs[i + 1]) / 2:
                return i
        return len(order)

    # -- text wrapping ----------------------------------------------------------------------
    def set_wrap(self, on):
        """Wrap cell text over several lines (taller rows) instead of cutting it with an
        ellipsis; False restores the single-line rows."""
        on = bool(on)
        if on == self._wrap:
            return
        self._wrap = on
        ls = self._fonts[0].metrics("linespace")
        self._rh = ls * (self._wrap_lines if on else 1) + 8
        self._layout()

    def _wrap_text(self, text, avail, fi):
        """text wrapped to at most _wrap_lines lines of avail pixels; the last line ends
        with an ellipsis when text remains. Cached."""
        key = (text, avail, fi)
        hit = self._wrap_cache.get(key)
        if hit is not None:
            return hit
        if len(self._wrap_cache) > 20000:
            self._wrap_cache.clear()
        want = self._wrap_lines
        words = []                      # (piece, space_before)
        for wd in text.replace("\n", " ").split(" "):
            if not wd:
                continue
            space = True
            while wd and self._text_width(wd, fi) > avail:   # a word wider than the
                n = max(1, self._prefix(wd, avail, fi)[0])   # column is cut to pieces
                words.append((wd[:n], space))
                wd = wd[n:]
                space = False           # continuation pieces join without a space
            if wd:
                words.append((wd, space))
        lines, cur, truncated = [], "", False
        for wd, space in words:
            trial = wd if not cur else (cur + " " + wd if space else cur + wd)
            if self._text_width(trial, fi) <= avail:
                cur = trial
            elif len(lines) + 1 < want:
                lines.append(cur)
                cur = wd
            else:
                truncated = True
                break
        if truncated:
            lines.append(self._fit(cur + "\u2026", avail, fi))
        else:
            lines.append(cur)
        self._wrap_cache[key] = lines
        return lines

    def _draw_wrapped_cell(self, surf, r, c, x, y, w, text, fill, fi, v):
        """One wrapped cell: up to _wrap_lines lines, top-aligned, with search hits lit."""
        avail = w - 2 * PAD
        lines = self._wrap_text(text, avail, fi)
        ls = self._fonts[fi].metrics("linespace")
        n = 0
        for i, ln in enumerate(lines):
            ly = y + 4 + ls // 2 + i * ls
            surf.cell.put((r, c, i), (x + PAD, ly), (ln, fill, fi))
            if self._hl_words and fi == 0 and v is not None \
                    and not isinstance(v, (bytes, Locator)):
                low = ascii_lower(ln)
                for word in self._hl_words:
                    start = 0
                    while True:
                        k = low.find(word, start)
                        if k < 0:
                            break
                        x0 = x + PAD + self._text_width(ln[:k], fi)
                        x1 = x0 + self._text_width(ln[k:k + len(word)], fi)
                        surf.hl.put((r, c, 4096 + n),
                                    (x0, ly - ls // 2 + 2, x1, ly + ls // 2 - 2),
                                    (K["highlight"], ""))
                        n += 1
                        start = k + len(word)

    def autosize_column(self, c):
        """Fit a column to its header and the rows in view."""
        w = self._text_width(self._header_text(c), 2) + 2 * PAD + 14 +             (FUNNEL_W if self.filterable(c) else 0)
        for values, _flags in self.visible_rows_data():
            if c < len(values):
                text, kind = cell_text(values[c], 200)
                text = self._formatted(values[c], c) or text
                w = max(w, self._text_width(text, 1 if kind == "null" else 0) + 2 * PAD + 2)
        self._widths[c] = max(MIN_COL_W, min(int(w), MAX_AUTOSIZE_W))
        self._user_sized.add(c)
        self._layout()

    def column_chooser(self):
        if self._chooser is not None and self._chooser.winfo_exists():
            self._chooser.lift()
            return self._chooser
        self._chooser = ColumnChooser(self)
        return self._chooser

    # -- public: inspector and workers ----------------------------------------------------------
    def set_inspector(self, show):
        show = bool(show)
        if show == self.inspector_visible():
            return
        if show:
            self._pane.add(self._insp, weight=0)
            self.after_idle(self._place_inspector_sash)
        else:
            self._pane.forget(self._insp)
        self._insp_key = None
        self._schedule()

    def inspector_visible(self):
        return str(self._insp) in [str(p) for p in self._pane.panes()]

    def inspector_rows(self):
        """(column, type, value) lines the inspector shows."""
        t = self._insp_tree
        return [(t.item(i, "text"),) + tuple(t.item(i, "values")[:2]) for i in t.get_children()]

    def cancel_fetches(self):
        self._runner.cancel()
        self._pending.clear()

    def worker_threads(self):
        """Worker threads still alive: the row reads' and the filter popover's."""
        return self._runner.threads() + self._frunner.threads()

    def destroy(self):
        self.close_filter_popover()
        pop, self._popover = self._popover, None
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass
        self._runner.close()
        self._frunner.close()
        self._tip_hide()
        for aid in (self._redraw_id, self._filter_after, self._drag_after, self._first_after):
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except tk.TclError:
                    pass
        self._redraw_id = self._filter_after = self._drag_after = self._first_after = None
        tk.Frame.destroy(self)

    # -- data -----------------------------------------------------------------------------------
    def _release_worker(self):
        hook = getattr(self._source, "release_thread", None)
        if hook is not None:
            hook()

    def _reset_rows(self, keep_top=False):
        self._gen += 1
        self._runner.cancel()
        self._cache.clear()
        self._pending.clear()
        self._failed.clear()
        self._interrupted.clear()
        self.load_error = ""
        self.notice = ""
        self._win = limits.get("grid_window_rows")
        # Let the filter chip/bar paint before the (possibly slow) COUNT(*)
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        n = self._source.row_count() if self._source is not None else 0
        self._exact = n is not None
        self._total = n if n is not None else self._win
        if not keep_top:
            self._top = 0
            if self._cur is not None:
                self._cur = (0, self._cur[1])
            self._anchor, self._sel = None, None
        self._insp_key = None
        self._clamp()
        self._schedule()

    def _store(self, w, rows):
        self._stored += 1
        self._cache[w] = rows
        self._cache.move_to_end(w)
        keep = max(4, limits.get("grid_cache_windows"))
        while len(self._cache) > keep:
            self._cache.popitem(last=False)
        win = self._win
        end = w * win + len(rows)
        if len(rows) < win:
            if not self._exact or end < self._total:  # (a window read ahead past the end: no)
                self._total, self._exact = end, True
        elif not self._exact and self._total < end + win:
            self._total = end + win
        if not self._sized and w == 0:
            self._sized = True
            self._autosize_initial(rows)

    def _request(self, w, batch=None):
        """Read window w (threaded sources: on the worker; with a batch list the job is added
        to it, for Runner.submit_many)."""
        if w in self._cache or w in self._pending or w in self._failed or self._source is None:
            return
        src, gen, win = self._source, self._gen, self._win
        start = w * win
        if not getattr(src, "threaded", False):
            try:
                self._store(w, list(src.rows(start, win)))
            except Exception as e:      # noqa: BLE001 - shown by the owner, not raised
                self._failed.add(w)
                self.load_error = "%s: %s" % (type(e).__name__, e)
            return
        self._pending.add(w)

        def done(rows, error):
            if gen != self._gen:
                return
            self._pending.discard(w)
            stopped = (gen, w) in self._interrupted
            self._interrupted.discard((gen, w))
            if error is not None:
                if not (stopped or self._retryable(src, error)):
                    self._failed.add(w)
                    self.load_error = "%s: %s" % (type(error).__name__, error)
            else:
                self._store(w, rows)
            self._schedule()
        job = ((gen, w), lambda: list(src.rows(start, win)), done)
        if batch is not None:
            batch.append(job)
        else:
            self._runner.submit(*job)

    @staticmethod
    def _retryable(src, error):
        hook = getattr(src, "retryable", None)
        try:
            return bool(hook(error)) if hook is not None else False
        except Exception:               # noqa: BLE001 - a hook that fails: not retryable
            return False

    def restart_reads(self):
        """The source can now read any window quickly (its position index is ready): the
        window read running now (maybe a long OFFSET scan) is stopped and the windows the view
        needs are read again, through the index."""
        key, th = self._runner.running()
        hook = getattr(self._source, "interrupt", None)
        if key is not None and th is not None and hook is not None and \
                key not in self._interrupted and key[0] == self._gen:
            self._interrupted.add(key)
            try:
                hook(th)
            except Exception:           # noqa: BLE001 - it then just finishes
                pass
        self._schedule()

    def waiting_seconds(self):
        """How long the rows the view is at have been read for (0 when not waiting)."""
        since = getattr(self, "_held_since", None)
        if not self._held or since is None:
            return 0.0
        return time.time() - since

    def _stop_stale_read(self, gen, needed):
        """Stop the window read running on the worker when the view no longer needs it."""
        key, th = self._runner.running()
        hook = getattr(self._source, "interrupt", None)
        if key is None or th is None or hook is None or key in self._interrupted:
            return
        if key[0] == gen and key[1] in needed:
            return
        self._interrupted.add(key)
        try:
            hook(th)
        except Exception:               # noqa: BLE001 - it then just finishes
            pass

    def _fetch_sync(self, lo, hi):
        """(values, flags) of rows lo..hi, from the cache or read now on this thread."""
        out = []
        r = lo
        win = self._win
        while r <= hi:
            w = r // win
            rows = self._cache.get(w)
            if rows is None:
                if self._source is None:
                    break
                rows = list(self._source.rows(w * win, win))
                self._store(w, rows)
            i = r - w * win
            take = rows[i:i + (hi - r + 1)]
            out.extend(take)
            if not take or (i + len(take) >= len(rows) and len(rows) < win):
                break
            r += len(take)
        return out

    def _autosize_initial(self, rows):
        n = len(self._cols)
        cap = 420 if n <= 8 else 320 if n <= 15 else 240
        sample = rows[:40]
        for c in range(n):
            if c in self._user_sized:
                continue
            longest = 0
            for values, _flags in sample:
                if c < len(values):
                    longest = max(longest, len(self._formatted(values[c], c)
                                               or cell_text(values[c], 120)[0]))
            want = longest * self._avgcw + 2 * PAD + 2
            self._widths[c] = max(self._header_width(c), min(int(want), cap))
        if self._frozen and 0 not in self._user_sized:
            # row locators get longer further down (rowid 7 .. 1,000,000): room for them
            n = self._source.row_count() if self._source is not None else None
            digits = len(str(n if n is not None else 10 ** 7))
            self._widths[0] = max(self._widths[0], 80, (digits + 3) * self._avgcw + 2 * PAD)
        self._layout()

    def _header_width(self, c):
        funnel = FUNNEL_W if self.filterable(c) else 0     # room for the filter funnel
        return max(MIN_COL_W, min(self._text_width(self._header_text(c), 2) + 2 * PAD + 14
                                  + funnel, 400 + funnel))

    def _header_text(self, c):
        ent = self._formatters.get(c)
        return self._cols[c] + ((" · " + ent[1]) if ent is not None and ent[1] else "")

    # -- layout and geometry ------------------------------------------------------------------
    def _layout(self):
        self._layout_gen += 1           # every item is placed again at the next redraw
        n = len(self._cols)
        if not self._col_order:
            self._col_order = list(range(self._frozen, n))
        else:
            seen, keep = set(), []
            for c in self._col_order:
                if c not in seen and self._frozen <= c < n:
                    seen.add(c)
                    keep.append(c)
            keep.extend(c for c in range(self._frozen, n) if c not in seen)
            self._col_order = keep
        self._order = [c for c in self._col_order if c not in self._hidden]
        self._pos = dict((c, k) for k, c in enumerate(self._order))
        xs = [0]
        for c in self._order:
            xs.append(xs[-1] + self._widths[c])
        self._xs = xs
        if self._frozen:
            for cv in (self._corner, self._fcv):
                cv.configure(width=self._widths[0])
                if not cv.winfo_manager():
                    cv.grid()
        else:
            for cv in (self._corner, self._fcv):
                if cv.winfo_manager():
                    cv.grid_remove()
        self._clamp()
        self._schedule()

    def _full_rows(self):
        return max(1, self._cv.winfo_height() // self._rh)

    def _page(self):
        return max(1, self._full_rows() - 1)

    def _clamp(self):
        max_top = max(0, self._total - self._full_rows())
        self._top = max(0, min(self._top, max_top))
        max_x = max(0, self._xs[-1] - max(1, self._cv.winfo_width()))
        self._xoff = max(0, min(self._xoff, max_x))

    def _row_at(self, y):
        top = self._drawn[0] if self._held else self._top     # the rows seen under the mouse
        r = top + int(y // self._rh)
        return r if 0 <= r < self._total and y >= 0 else None

    def _col_at(self, cv, x):
        if cv in (self._fcv, self._corner):
            return 0 if self._frozen else None
        k = bisect.bisect_right(self._xs, x + self._xoff) - 1
        return self._order[k] if 0 <= k < len(self._order) else None

    def _border_at(self, cv, x):
        """Column whose right header border is under x (for resizing), else None."""
        if cv is self._corner:
            return 0 if self._frozen and abs(x - self._widths[0]) <= BORDER_GRIP else None
        X = x + self._xoff
        k = bisect.bisect_left(self._xs, X - BORDER_GRIP)
        if 1 <= k < len(self._xs) and abs(self._xs[k] - X) <= BORDER_GRIP:
            return self._order[k - 1]
        return None

    def _ensure_visible(self, row, col):
        full = self._full_rows()
        if row < self._top:
            self._top = row
        elif row >= self._top + full:
            self._top = row - full + 1
        if col is not None and col in self._pos:
            k = self._pos[col]
            x0, x1 = self._xs[k], self._xs[k + 1]
            w = max(1, self._cv.winfo_width())
            if x0 < self._xoff:
                self._xoff = x0
            elif x1 > self._xoff + w:
                self._xoff = min(x0, x1 - w)
        self._clamp()

    # -- drawing --------------------------------------------------------------------------------
    def _schedule(self):
        if self._redraw_id is None:
            try:
                self._redraw_id = self.after_idle(self._redraw)
            except tk.TclError:
                self._redraw_id = None

    def _prefix(self, text, avail, fi):
        """(n, width): the most leading characters of text whose widths add up to at most
        avail pixels in font fi, and that width (see CharWidths: a cell cut to fit took ~8 Tk
        measure calls, and with emoji or other scripts each call is slow - the first draw of
        a 51-column WhatsApp table took 400 ms)."""
        table = self._charw[fi]
        w = 0
        for i, ch in enumerate(text):
            cw = table.get(ch)
            if cw is None:
                cw = self._cw.width(ch, fi)
            if w + cw > avail:
                return i, w
            w += cw
        return len(text), w

    def widths_learnt(self):
        """Characters were measured (CharWidths): fit the text in view again."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._fit_cache.clear()
        self._measure_cache.clear()
        self._schedule()

    def _fit(self, text, avail, fi):
        """text cut to fit avail pixels in font fi (with an ellipsis), cached."""
        if avail <= 0 or not text:
            return ""
        key = (text, avail, fi)
        hit = self._fit_cache.get(key)
        if hit is not None:
            return hit
        if len(text) * self._maxcw[fi] <= avail:
            res = text
        else:
            n, _w = self._prefix(text, avail, fi)
            # a font system that kerns ASCII: the cut is checked against Tk (quick for
            # ASCII; other text is not measured by Tk here, see CharWidths)
            check = not self._exactw[fi] and text.isascii()
            if n == len(text) and (not check or self._text_width(text, fi) <= avail):
                res = text
            else:
                ell = self._ellw[fi]
                lo = self._prefix(text, avail - ell, fi)[0] if avail > ell else 0
                while check and lo and self._text_width(text[:lo], fi) + ell > avail:
                    lo -= 1
                res = (text[:lo] + "\u2026") if lo else ("\u2026" if ell <= avail else "")
        if len(self._fit_cache) > 40000:
            self._fit_cache.clear()
        self._fit_cache[key] = res
        return res

    def _text_width(self, text, fi):
        key = (text, fi)
        w = self._measure_cache.get(key)
        if w is None:
            if len(self._measure_cache) > 50000:
                self._measure_cache.clear()
            if self._exactw[fi] or not text.isascii():
                w = self._prefix(text, 1 << 30, fi)[1]
            else:
                w = self._fonts[fi].measure(text)
            self._measure_cache[key] = w
        return w

    def _cell(self, v, c=None):
        """(text, fill, font index) for a value (of column c: its formatter applies)."""
        if v is _MISSING:
            return "", C["text2"], 0
        if isinstance(v, Locator):
            return str(v), C["text2"], 0
        if c is not None and c in self._formatters:
            text = self._formatted(v, c)
            if text is not None:
                return text, self._formatter_colors.get(c, C["green"]), 0
        text, kind = cell_text(v)
        if kind == "null":
            return text, C["text2"], 1
        if kind == "blob":
            return text, C["purple"], 0
        if kind == "invalid":
            return text, C["red"], 0
        return text, C["text"], 0

    def _row_bg(self, r, flags, tint=None):
        if self._sel is not None and self._sel[0] <= r <= self._sel[1]:
            return C["tsel"]
        tag = row_flag_tag(flags)
        if tag in ROW_FLAG_BG:
            return ROW_FLAG_BG[tag]
        if tint:
            return tint
        return C["alt"] if r % 2 else C["bg"]

    def _style(self, r, d):
        """(background, marker) the owner's row_style gives a loaded row; a failing callback
        marks nothing rather than breaking the redraw."""
        if d is None or self.row_style is None:
            return None, None
        try:
            return self.row_style(r, d[0], d[1]) or (None, None)
        except Exception:       # noqa: BLE001 - marks are a courtesy
            return None, None

    def _redraw(self, budget=REDRAW_BUDGET):
        self._redraw_id = None
        self._t_redraw = time.perf_counter()
        cv = self._cv
        W, H = cv.winfo_width(), cv.winfo_height()
        if W <= 1 or H <= 1:
            return
        self._clamp()
        rh = self._rh
        nvis = (H + rh - 1) // rh
        first = self._top
        end = min(first + nvis, self._total)
        if first != self._last_top:
            self._direction = 1 if first > self._last_top else -1
            self._last_top = first
        # rows: request the windows in view, then those just ahead in the scroll direction
        # (and one behind); an empty source is still read once, so it can say why it is empty
        if self._source is not None and (end > first or not self._cache):
            self._request_windows(first, end)
            end = min(end, self._total)     # a short window may have ended the rows
        data = [self.row_data(r) for r in range(first, end)]
        missing = any(d is None for d in data)
        frame = self._shown_rows
        reading = bool(self._pending) or self._runner.busy() or self._drag_after is not None
        target = (first, end)
        held = False
        if missing and reading and frame is not None and frame[0] is self._source:
            # rows at the new position are still being read: draw the rows drawn last again
            # (in the current layout); the scrollbar and the status show the new position
            first, end, data = frame[1], frame[2], frame[3]
            held = True
        elif missing and reading and frame is not None and frame[0] is not self._source \
                and self._source is not None:
            # another source's first rows: leave the last table in view a moment rather than
            # draw empty rows (its rows usually arrive within milliseconds)
            left = self._source_t0 + limits.get("grid_first_rows_wait_ms") / 1000.0 - time.time()
            if left > 0:
                self._held = True
                self._target = (first, end)
                self.frames["held"] += 1
                if self._first_after is None:
                    self._first_after = self.after(int(left * 1000) + 1, self._first_rows_due)
                return
        if held and not self._held:
            self._held_since = time.time()      # waiting_seconds()
        self._held = held
        if held:
            self.frames["held"] += 1
        elif missing and end > first:
            first_frame = frame is None or frame[0] is not self._source
            self.frames["first" if first_frame else "empty"] += 1
        else:
            self.frames["drawn"] += 1
        styles = [self._style(first + i, d) for i, d in enumerate(data)]
        if self._order and max(self._order) >= len(self._widths):
            self._layout()              # the columns changed since the last layout
        # scrolling columns in view: (column, x in canvas coordinates, width)
        k = max(0, bisect.bisect_right(self._xs, self._xoff) - 1)
        vis = []
        while k < len(self._order) and self._xs[k] < self._xoff + W:
            vis.append((self._order[k], self._xs[k], self._widths[self._order[k]]))
            k += 1
        if abs(first - self._base) > REBASE:
            self._base = first
            self._layout_gen += 1       # keep canvas coordinates small: place everything anew
        layout = (self._layout_gen, self._base, W)
        span = max(self._xs[-1], W)
        oy = (first - self._base) * rh
        self._draw_header(self._surf[self._hdr], vis, self._xoff, layout)
        # a redraw past its time budget stops after a row and goes on at the next turn of the
        # event loop (the rows drawn already cost nothing then): cells of emoji and other
        # scripts are slow for Tk to lay out, and a full first draw held it over 100 ms
        stop = self._t_redraw + budget if budget else None
        # each continuation draws at least one row more than the redraw before it
        least = (self._rows_cut or 0) + 1 if (first, end) == self._cut_view else 1
        self._rows_cut = None
        cleared = self._draw_rows(self._surf[cv], vis, first, data, span, self._xoff, oy, layout,
                                  False, styles, stop, least)
        self._place_entries(vis, cleared)
        drawn = data if self._rows_cut is None else data[:self._rows_cut]
        if self._frozen:
            fw = self._widths[0]
            self._draw_header(self._surf[self._corner], [(0, 0, fw)], 0, layout, frozen=True)
            self._draw_rows(self._surf[self._fcv], [(0, 0, fw)], first, drawn, fw, 0, oy, layout,
                            True, styles)
        self._cut_view = (first, end) if self._rows_cut is not None else None
        if self._rows_cut is not None:
            self.frames["cut"] += 1
            self._schedule()
        total = max(1, self._total)
        full = self._full_rows()
        self._vsb.set(target[0] / float(total), min(1.0, (target[0] + full) / float(total)))
        tw = max(1, self._xs[-1])
        self._hsb.set(self._xoff / float(tw), min(1.0, (self._xoff + W) / float(tw)))
        self._drawn = (first, end, [c for c, _x, _w in vis])
        self._target = target
        if not held and self._source is not None and end > first and (
                not missing or frame is None or frame[0] is not self._source):
            self._shown_rows = (self._source, first, end, data)
        if self.inspector_visible():
            self._refresh_inspector()
        self._notify_view(data)

    def _notify_view(self, data):
        first, end = self._drawn[0], self._drawn[1]
        loaded = sum(1 for d in data if d is not None)
        key = (self._gen, self._stored, first, end, loaded, self._total, self._exact,
               tuple(self._drawn[2]), self._sel, self._cur, self.load_error, self._held,
               self._target, self.notice)
        if key != self._view_key:
            if self._tip is not None and (first, self._xoff) != self._tip_view:
                self._tip_hide()            # the rows moved under the tip
            self._tip_view = (first, self._xoff)
            self._view_key = key
            self.filter_bar.set_count(self.filter_count_text())
            self._update_empty_state()
            if self.on_view_change is not None:
                self.on_view_change(self)

    def _request_windows(self, first, end):
        """Ask for the windows of rows first..end, then (threaded sources) those ahead in the
        scroll direction and one behind. While the scrollbar is being dragged, windows not
        read yet are asked for once it has rested a moment."""
        win = self._win
        gen = self._gen
        visible = list(range(first // win, max(end - 1, first) // win + 1))
        needed = set(visible)
        threaded = getattr(self._source, "threaded", False)
        if threaded:
            last = (self._total - 1) // win if self._total > 0 else 0
            depth = limits.get("grid_prefetch_windows")
            if self._direction > 0:
                ahead = [visible[-1] + i for i in range(1, depth + 1)]
                behind = [visible[0] - 1]
            else:
                ahead = [visible[0] - i for i in range(1, depth + 1)]
                behind = [visible[-1] + 1]
            # an unknown row count: nothing past the window after the view
            top = last if self._exact else visible[-1] + 1
            extra = [w for w in ahead + behind if 0 <= w <= top]
            needed.update(extra)
        else:
            extra = []
        for key in self._runner.discard(lambda k: k[0] == gen and k[1] in needed):
            self._pending.discard(key[1])
        if threaded:
            self._stop_stale_read(gen, needed)
            if time.time() < self._drag_until and any(w not in self._cache for w in visible):
                # dragging: read once the thumb rests (the newest position wins)
                if self._drag_after is None:
                    delay = max(1, int((self._drag_until - time.time()) * 1000))
                    self._drag_after = self.after(delay, self._drag_rested)
                return
        # the Runner reads the newest request first: queue the visible windows last, all at
        # once so the worker cannot start on a window read ahead first
        batch = []
        for w in reversed(extra):
            self._request(w, batch)
        for w in reversed(visible):
            self._request(w, batch)
        self._runner.submit_many(batch)
        for w in visible:
            if w in self._cache:
                self._cache.move_to_end(w)

    def _drag_rested(self):
        self._drag_after = None
        self._schedule()

    def _first_rows_due(self):
        self._first_after = None
        self._schedule()

    def _draw_header(self, surf, cols, ox, layout, frozen=False):
        hh, fh = self._hh, self._fh
        surf.begin(layout, ox, 0)
        sort_c, sort_desc = self._sort
        for c, x, w in cols:
            on = self._filter_on(c)
            surf.hdrbg.put(c, (x, 0, x + w - 1, hh - 1),
                           (K["accent_soft"] if on else C["bg3"], C["border"]))
            arrow = (" \u25bc" if sort_desc else " \u25b2") if c == sort_c else ""
            mark = self._marks.get(c, "")
            funnel = self._has_funnel(c, w)
            avail = w - 2 * PAD - (FUNNEL_W if funnel else 0)
            surf.hdrtext.put(c, (x + PAD, hh // 2),
                             (self._fit(self._header_text(c) + mark + arrow, avail, 2), C["text"],
                              2))
            if funnel:
                fx, fy = x + w - FUNNEL_W // 2 - 4, hh // 2 - 5
                hover = self._funnel_hover == c
                fill = K["accent"] if on else (K["primary_soft"] if hover else "")
                edge = K["accent"] if on else (K["primary"] if hover else K["muted_text"])
                surf.funnel.put(c, funnel_points(fx, fy), (fill, edge, fx, fy))
        if frozen and self._inline:
            w = cols[0][2]
            surf.hdrbg.put("filter", (0, hh, w - 1, hh + fh - 1), (C["bg2"], C["border"]))
            surf.fixed.put("filter", (PAD, hh + fh // 2), ("Filter \u25b8", C["text2"], 3))
        surf.end()

    def _filter_on(self, c):
        """True when column c has a filter in force (not one that cannot be used)."""
        var = self._fvars.get(c)
        return var is not None and bool(var.get().strip()) and c not in self._filter_errors

    def _has_funnel(self, c, w):
        return self._source is not None and self.filterable(c) and w >= FUNNEL_MIN_COL

    def funnel_items(self):
        """{column: (fill, outline)} of the funnels drawn in the headers now."""
        out = {}
        for cv in (self._hdr, self._corner):
            for c, ent in self._surf[cv].funnel.items.items():
                out[c] = (ent[1][0], ent[1][1])
        return out

    def header_background(self, c):
        for cv in (self._hdr, self._corner):
            ent = self._surf[cv].hdrbg.items.get(c)
            if ent is not None:
                return ent[1][0]
        return None

    def highlight_items(self):
        """{(row, column, n): (x0, x1)} of the search matches highlighted in the cells now."""
        out = {}
        for cv in (self._cv, self._fcv):
            for k, ent in self._surf[cv].hl.items.items():
                out[k] = (ent[1][2], ent[1][3])
        return out

    def _funnel_at(self, cv, x, y):
        """The column whose header funnel is under (x, y) of a header canvas, else None."""
        if y >= self._hh:
            return None
        c = self._col_at(cv, x)
        if c is None:
            return None
        if cv is self._corner:
            right = self._widths[0]
        else:
            k = self._pos.get(c)
            if k is None:
                return None
            right = self._xs[k + 1] - self._xoff
        if not self._has_funnel(c, self._widths[c]):
            return None
        return c if right - FUNNEL_W - 4 <= x <= right - BORDER_GRIP - 1 else None

    def _set_funnel_hover(self, c):
        if c != self._funnel_hover:
            self._funnel_hover = c
            self._schedule()

    def _highlight(self, surf, r, c, x, y, shown, fi):
        """Amber behind each occurrence of the search's words in a cell's drawn text."""
        low = ascii_lower(shown)
        n = 0
        for w in self._hl_words:
            start = 0
            while True:
                i = low.find(w, start)
                if i < 0:
                    break
                x0 = x + self._text_width(shown[:i], fi)
                x1 = x0 + self._text_width(shown[i:i + len(w)], fi)
                surf.hl.put((r, c, n), (x0, y + 3, x1, y + self._rh - 3),
                            (K["highlight"], "", x0, x1))
                n += 1
                start = i + len(w)

    def _draw_rows(self, surf, cols, first, data, span, ox, oy, layout, frozen, styles,
                   stop=None, least=1):
        """Draw rows first.. (`data`, with their row_style `styles`) for the columns `cols`
        [(column, x, width)] on one canvas scrolled to (ox, oy). Returns True when every item
        was placed anew. stop: a time.perf_counter() after which no further row is drawn, once
        `least` rows are (self._rows_cut then says how many were)."""
        rh, base = self._rh, self._base
        cleared = surf.begin(layout, ox, oy)
        grid_c = C["bg4"]
        hl = bool(self._hl_words)
        v = None
        cur_r, cur_c = self._cur if self._cur is not None else (None, None)
        cur_box = None
        for i, d in enumerate(data):
            if stop is not None and i >= least and time.perf_counter() > stop:
                self._rows_cut = i
                break
            r = first + i
            y = (r - base) * rh
            values, flags = d if d is not None else (None, ())
            tint, marker = styles[i]
            surf.rowbg.put(r, (0, y, span, y + rh), (self._row_bg(r, flags, tint), ""))
            if frozen and marker:
                surf.marker.put(r, (1, y + 3, 1 + MARKER_W, y + rh - 3), (marker, marker))
            mid = y + rh // 2
            for c, x, w in cols:
                if values is None:
                    if not frozen:
                        continue
                    text, fill, fi = "\u2026", C["text2"], 0
                else:
                    v = values[c] if c < len(values) else _MISSING
                    text, fill, fi = self._cell(v, c)
                    if frozen and "damaged_record" in flags:
                        text, fill = "\u26a0 " + text, C["red"]
                avail = w - 2 * PAD
                if values is not None and not frozen and isinstance(v, str) \
                        and c not in self._formatters:
                    badge = size_badge(v)
                    if badge:
                        bw = self._text_width(badge, 3)
                        if bw + 24 < avail:     # room left for some of the text itself
                            surf.badge.put((r, c), (x + w - PAD, mid), (badge, C["text2"], 3))
                            avail -= bw + 6
                if text:
                    if self._wrap and not frozen:
                        self._draw_wrapped_cell(surf, r, c, x, y, w, text, fill, fi, v)
                    else:
                        shown = self._fit(text, avail, fi)
                        surf.cell.put((r, c), (x + PAD, mid), (shown, fill, fi))
                        if hl and fi == 0 and values is not None and v is not None \
                                and not isinstance(v, (bytes, Locator)):
                            self._highlight(surf, r, c, x + PAD, y, shown, fi)
                if r == cur_r and c == cur_c:
                    cur_box = (x + 1, y + 1, x + w - 2, y + rh - 2)
            surf.hline.put(r, (0, y + rh - 1, span, y + rh - 1), (grid_c,))
        # column lines run over every row the canvas coordinates cover
        y0 = max(-base, -BAND) * rh
        y1 = min(self._total - base, BAND) * rh
        for c, x, w in cols:
            surf.vline.put(c, (x + w - 1, y0, x + w - 1, y1), (grid_c, y0, y1))
        if not frozen and not data and self._exact and self._total == 0 \
                and self._source is not None and not self._no_match():
            surf.fixed.put("empty", (ox + PAD + 4, oy + rh), ("No rows", C["text2"], 1, ox, oy))
        surf.end()
        surf.set_cur(cur_box)
        return cleared

    def _place_entries(self, vis, cleared):
        """Show the filter entries of the columns in view. They sit at their column's position
        on the header canvas and scroll with it, so only entries coming into view are placed."""
        cv = self._hdr
        if cleared or not self._inline:
            for c in self._entry_shown:
                cv.itemconfigure(self._entries[c][1], state="hidden")
            self._entry_shown = set()
        if not self._inline:
            return
        want = set()
        for c, x, w in vis:
            want.add(c)
            if c not in self._entry_shown:
                _e, wid, _tip = self._entry(c)
                cv.coords(wid, x, self._hh)
                cv.itemconfigure(wid, width=w, height=self._fh, state="normal")
                self._entry_shown.add(c)
        for c in self._entry_shown - want:
            cv.itemconfigure(self._entries[c][1], state="hidden")
        self._entry_shown &= want

    # -- filter entries ------------------------------------------------------------------------
    def _fvar(self, c):
        var = self._fvars.get(c)
        if var is None:
            var = self._fvars[c] = tk.StringVar(self)
            var.trace_add("write", lambda *_a: self._debounce_filters())
        return var

    def _entry(self, c):
        ent = self._entries.get(c)
        if ent is not None:
            return ent
        e = ttk.Entry(self._hdr, textvariable=self._fvar(c), style="GridFilter.TEntry",
                      font=self._fonts[3])
        wid = self._hdr.create_window(0, 0, window=e, anchor="nw", state="hidden",
                                      tags=("filter",))
        tip = ToolTip(e, FILTER_HINT)
        add_placeholder(e, self._fvar(c), "filter…", font=self._fonts[3])
        e.bind("<Return>", lambda ev: (self.apply_filters(), "break")[1])
        e.bind("<Escape>", lambda ev, c=c: (self.set_filter_text(c, ""), "break")[1])
        e.bind("<Down>", lambda ev: (self._cv.focus_set(), "break")[1])
        e.bind("<FocusIn>", lambda ev, c=c: self._entry_focus(c))
        self._bind_wheel(e)
        ent = self._entries[c] = (e, wid, tip)
        self._style_entry(c)
        return ent

    def _entry_focus(self, c):
        if c in self._pos:
            k = self._pos[c]
            if self._xs[k] < self._xoff or self._xs[k + 1] > self._xoff + self._cv.winfo_width():
                self._ensure_visible(self._top, c)
                self._schedule()

    def _style_entry(self, c):
        e, _wid, tip = self._entries[c]
        msg = self._filter_errors.get(c)
        var = self._fvars.get(c)
        on = var is not None and bool(var.get().strip())
        e.configure(style="GridFilterBad.TEntry" if msg else (
            "GridFilterOn.TEntry" if on else "GridFilter.TEntry"))
        tip.text = ("Cannot use this filter: " + msg) if msg else FILTER_HINT

    def _debounce_filters(self):
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
        self._filter_after = self.after(self.filter_delay, self.apply_filters)

    # -- mouse ------------------------------------------------------------------------------------
    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", self._on_wheel)
        w.bind("<Shift-MouseWheel>", lambda e: self._on_wheel(e, True))
        w.bind("<Button-4>", lambda e: (self.scroll_rows(-3), "break")[1])
        w.bind("<Button-5>", lambda e: (self.scroll_rows(3), "break")[1])
        w.bind("<Shift-Button-4>", lambda e: (self.scroll_x(-60), "break")[1])
        w.bind("<Shift-Button-5>", lambda e: (self.scroll_x(60), "break")[1])

    def _tip_motion(self, e, cv):
        cell = (self._row_at(e.y), self._col_at(cv, e.x))
        if cell == self._tip_cell:
            return
        self._tip_hide()
        self._tip_cell = cell
        if None not in cell:
            self._tip_after = self.after(500, lambda: self.show_cell_tip(cell[0], cell[1],
                                                                         e.x_root, e.y_root))

    def show_cell_tip(self, row, col, x_root, y_root):
        """Show the whole text of a cell that is drawn cut off; returns the text shown (None
        when the cell fits its column)."""
        self._tip_after = None
        v = self._value_at(row, col)
        if v is _MISSING:
            return None
        text, _fill, fi = self._cell(v, col)
        big = isinstance(v, str) and bool(size_badge(v))
        if isinstance(v, Locator):
            full = str(v)
        elif isinstance(v, str):
            full = tip_preview(v)
        else:
            full = cell_text(v, limits.get("cell_tip_chars"))[0]
        shown = self._formatted(v, col)
        if shown is not None:
            full = "%s\nraw: %s" % (shown, full)     # a formatted cell always tells its raw value
        elif not big and len(full) <= len(text) and \
                self._fit(text, self._widths[col] - 2 * PAD, fi) == text:
            return None
        if big:
            full += "\n\n[%s]  Shift+Enter or right-click › View value… to see all" \
                % size_badge(v)
        self._tip_hide(keep_cell=True)
        tw = self._tip = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=full, bg=K["tip"], fg=K["tip_text"], font=F["mono"], padx=8,
                 pady=5, relief="solid", bd=1, wraplength=600, justify="left").pack()
        tw.update_idletasks()
        if self._tip is not tw:
            return None                 # a redraw meanwhile moved the rows and hid the tip
        x = min(x_root + 15, tw.winfo_screenwidth() - tw.winfo_reqwidth() - 10)
        y = y_root + 20
        if y + tw.winfo_reqheight() > tw.winfo_screenheight() - 10:
            y = y_root - tw.winfo_reqheight() - 5
        tw.wm_geometry("+%d+%d" % (x, y))
        return full

    def _pointer_inside(self):
        """Whether the grid is shown and the mouse is over it."""
        try:
            if not self.winfo_viewable():
                return False
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
        except tk.TclError:
            return False
        while w is not None:
            if w is self:
                return True
            w = w.master
        return False

    def _tip_hide(self, _e=None, keep_cell=False):
        if self._tip_after is not None:
            self.after_cancel(self._tip_after)
            self._tip_after = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        if not keep_cell:
            self._tip_cell = None

    def _on_wheel(self, e, horizontal=False):
        self._tip_hide()
        d = e.delta
        steps = -int(d / 120) if abs(d) >= 120 else (-1 if d > 0 else 1)
        if horizontal:
            self.scroll_x(steps * 60)
        else:
            self.scroll_rows(steps * 3)
        return "break"

    def _on_vscroll(self, *args):
        if not args:
            return
        try:
            if args[0] == "moveto":
                self._top = int(float(args[1]) * self._total)
                self._drag_until = time.time() + limits.get("grid_drag_debounce_ms") / 1000.0
            elif args[0] == "scroll":
                n = int(args[1])
                self._top += n * (self._page() if args[2] == "pages" else 1)
        except (ValueError, IndexError):
            return
        self._clamp()
        self._schedule()

    def drag_to(self, fraction):
        """Move the view as a drag of the scrollbar thumb to `fraction` does."""
        self._on_vscroll("moveto", str(fraction))

    def _on_hscroll(self, *args):
        if not args:
            return
        if args[0] == "moveto":
            self._xoff = int(float(args[1]) * self._xs[-1])
        elif args[0] == "scroll":
            n = int(args[1])
            self._xoff += n * (max(40, self._cv.winfo_width() - 40) if args[2] == "pages" else 40)
        self._clamp()
        self._schedule()

    def _on_motion(self, e, cv):
        if self._drag is not None:
            return
        grip = e.y < self._hh and self._border_at(cv, e.x) is not None
        funnel = None if grip else self._funnel_at(cv, e.x, e.y)
        self._set_funnel_hover(funnel)
        cursor = "sb_h_double_arrow" if grip else ("hand2" if funnel is not None else "")
        if cv.cget("cursor") != cursor:
            cv.configure(cursor=cursor)
        # a header with a mark (e.g. the link sign of a column other tables refer to) says
        # what the mark means on hover
        if cv in (self._hdr, self._corner) and e.y < self._hh and not grip and funnel is None:
            c = self._col_at(cv, e.x)
            text = self.mark_tips.get(self._marks.get(c, "").strip()) if c is not None else None
            key = ("hdr", c)
            if text and self._tip_cell != key:
                self._tip_hide()
                self._tip_cell = key
                self._tip_after = self.after(500, lambda: self._show_text_tip(
                    "%s: %s" % (self._cols[c], text), e.x_root, e.y_root))
            elif not text and isinstance(self._tip_cell, tuple) and                     self._tip_cell[:1] == ("hdr",):
                self._tip_hide()

    def _show_text_tip(self, text, x_root, y_root):
        self._tip_after = None
        if not self._pointer_inside():
            return                      # the mouse left (or the grid was hidden) meanwhile
        self._tip_hide(keep_cell=True)
        tw = self._tip = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=text, bg=K["tip"], fg=K["tip_text"], font=F["body"], padx=8,
                 pady=5, relief="solid", bd=1, wraplength=420, justify="left").pack()
        tw.update_idletasks()
        x = min(x_root + 15, tw.winfo_screenwidth() - tw.winfo_reqwidth() - 10)
        tw.wm_geometry("+%d+%d" % (x, y_root + 20))

    def _on_press(self, e, cv, shift):
        self._tip_hide()
        self.close_filter_popover()
        self._cv.focus_set()
        if cv in (self._hdr, self._corner):
            if e.y < self._hh:
                b = self._border_at(cv, e.x)
                if b is not None:
                    self._drag = (b, e.x_root, self._widths[b])
                else:
                    f = self._funnel_at(cv, e.x, e.y)
                    self._hdr_press = ("funnel", f) if f is not None else self._col_at(cv, e.x)
                    c = self._hdr_press
                    self._hdr_move = [c, e.x_root, False] \
                        if isinstance(c, int) and c >= self._frozen else None
            return
        row = self._row_at(e.y)
        if row is not None:
            self.set_current_cell(row, self._col_at(cv, e.x), extend=shift)

    def _on_drag(self, e, cv):
        if self._drag is not None:
            c, x0, w0 = self._drag
            self._widths[c] = max(MIN_COL_W, w0 + e.x_root - x0)
            self._user_sized.add(c)
            self._layout()
            return
        if self._hdr_move is not None and cv in (self._hdr, self._corner):
            c, x0, moved = self._hdr_move
            if not moved and abs(e.x_root - x0) < 6:
                return                          # still a click: sorting happens on release
            self._hdr_move[2] = True
            self._move_to_vpos(c, self._drop_vpos(e.x))
            return
        if cv in (self._cv, self._fcv) and self._anchor is not None:
            row = self._row_at(min(max(e.y, 0), cv.winfo_height() - 1))
            if row is not None:
                self.set_current_cell(row, self._col_at(cv, e.x), extend=True)

    def _on_release(self, e, cv):
        c, self._hdr_press = self._hdr_press, None
        if self._drag is not None:
            self._drag = None
            return
        hm, self._hdr_move = self._hdr_move, None
        if hm is not None and hm[2]:
            return                              # it reordered a column: not a click
        if isinstance(c, tuple):
            if self._funnel_at(cv, e.x, e.y) == c[1]:
                self.open_filter_popover(c[1])
            return
        if c is not None and e.y < self._hh and self._col_at(cv, e.x) == c:
            self.sort_by(c)

    def _on_double(self, e, cv):
        if cv in (self._hdr, self._corner):
            b = self._border_at(cv, e.x) if e.y < self._hh else None
            if b is not None:
                self._drag = None
                self.autosize_column(b)
            return
        row = self._row_at(e.y)
        if row is not None:
            self.set_current_cell(row, self._col_at(cv, e.x))
            self._open_current()

    def _on_right(self, e, cv):
        self._tip_hide()
        if cv in (self._hdr, self._corner):
            c = self._col_at(cv, e.x)
            if c is None or e.y >= self._hh:
                return
            menu = self.build_header_menu(c)
        else:
            row, col = self._row_at(e.y), self._col_at(cv, e.x)
            if row is None:
                return
            if self._sel is None or not self._sel[0] <= row <= self._sel[1]:
                self.set_current_cell(row, col)
            else:
                self._cur = (row, col if col is not None else self._cur[1])
                self._schedule()
            menu = self.build_context_menu(row, col)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # -- actions ----------------------------------------------------------------------------------
    def sort_by(self, c, desc=None):
        """Sort by column c (toggles the direction when it is already the sort column)."""
        if self._source is None or c is None:
            return
        if desc is None:
            desc = (not self._sort[1]) if self._sort[0] == c else False
        if hasattr(self._source, "sort"):
            self._source.sort(self._cols[c], desc)
        self._sort = (c, desc)
        self._reset_rows()
        self._push_history()
        self._update_filter_bar()
        if self.on_sort is not None:
            self.on_sort(self._cols[c], desc)

    def _open_current(self):
        if self._cur is None or self.on_open_row is None:
            return
        d = self.row_data(self._cur[0])
        if d is not None:
            self.on_open_row(self._cur[0], d[0])

    def view_value(self, row=None, col=None):
        """Open the whole value of a cell (the current one by default) in a ValueViewer;
        returns the viewer, or None for a cell without a text value (BLOBs have the BLOB
        viewer)."""
        if row is None or col is None:
            if self._cur is None:
                return None
            row, col = self._cur
        v = self._value_at(row, col)
        if v is _MISSING or isinstance(v, Locator):
            return None
        from value_viewer import ValueViewer, value_text
        if value_text(v) is None:
            return None
        loc = self._value_at(row, 0) if self._frozen else None
        where = " — row %s" % loc.display() if isinstance(loc, Locator) else \
            " — row %s" % format(row + 1, ",")
        # which table and database (the owner says: grid.context)
        ctx = self.context() if callable(self.context) else self.context
        return ValueViewer(self, v, "%s%s%s" % (self._cols[col], where,
                                                " (%s)" % ctx if ctx else ""))

    def _value_at(self, row, col):
        d = self.row_data(row)
        if d is None or col is None or col >= len(d[0]):
            return _MISSING
        return d[0][col]

    def build_context_menu(self, row, col):
        """The right-click menu for a cell (not posted)."""
        if self._menu is not None:
            self._menu.destroy()
        m = self._menu = tk.Menu(self, tearoff=0)
        v = self._value_at(row, col)
        n = (self._sel[1] - self._sel[0] + 1) if self._sel else 1
        rows = "row" if n == 1 else "%s rows" % format(n, ",")
        m.add_command(label="Copy cell", accelerator="Ctrl+C", command=self.copy_cell)
        if col is not None and col in self._formatters:
            m.add_command(label="Copy raw value", command=self.copy_raw_cell)
        m.add_command(label="Copy %s as TSV" % rows, accelerator="Ctrl+Shift+C",
                      command=lambda: self.copy_rows("tsv"))
        m.add_command(label="Copy %s as CSV" % rows, command=lambda: self.copy_rows("csv"))
        m.add_command(label="Copy %s as JSON" % rows, command=lambda: self.copy_rows("json"))
        # only what applies to this cell is offered (no greyed-out items)
        m.add_separator()
        self._quick_filter_items(m, row, col, v)
        m.add_separator()
        if self.on_open_row and v is not _MISSING:
            m.add_command(label="Open row detail", accelerator="Enter",
                          command=self._open_current)
        blob = isinstance(v, bytes) and not isinstance(v, InvalidText)
        if v is not _MISSING and not blob and not isinstance(v, Locator):
            m.add_command(label="View value\u2026", accelerator="Shift+Enter",
                          command=lambda: self.view_value(row, col))
        if blob and self.on_open_blob:
            m.add_command(label="Inspect BLOB\u2026",
                          command=lambda: self.on_open_blob(row, self._cols[col], v))
        m.add_separator()
        if col is not None and col >= self._frozen:
            m.add_command(label="Hide column", command=lambda: self.hide_column(col))
        m.add_command(label="Column chooser\u2026", command=self.column_chooser)
        if self.on_context_menu is not None:
            self.on_context_menu(m, row, col)
        tidy_menu(m)
        return m

    def _quick_filter_items(self, m, row, col, v):
        """The cell menu's filter entries: only what applies to this cell."""
        if not self.filterable(col) or v is _MISSING or isinstance(v, Locator):
            if self.active_filters():
                m.add_command(label="Clear all filters", command=self.clear_filters)
            return
        name = self._cols[col]
        if value_expr(v):
            m.add_command(label="Filter to this value",
                          command=lambda: self.quick_filter(col, "only", v))
        if colfilter.expressible(v):
            m.add_command(label="Exclude this value",
                          command=lambda: self.quick_filter(col, "exclude", v))
        ct = self.column_type(col)
        if ct is not None and ct.kind == "date" and tl.to_utc(v, ct.date_kind) is not None:
            m.add_command(label="Filter to this day",
                          command=lambda: self.quick_filter(col, "day", v))
            m.add_command(label="Filter to this hour",
                          command=lambda: self.quick_filter(col, "hour", v))
        n = (self._sel[1] - self._sel[0] + 1) if self._sel else 1
        if n > 1:
            m.add_command(label="Filter to these values (%s rows)" % format(n, ","),
                          command=lambda: self._filter_to_selected(col))
        same = tk.Menu(m, tearoff=0)
        d = self.row_data(row)
        for c in self.displayed_columns():
            if c == col or not self.filterable(c) or d is None or c >= len(d[0]):
                continue
            cv = d[0][c]
            if value_expr(cv) is None:
                continue
            same.add_command(label="%s (%s)" % (self._cols[c], colfilter.short_value(cv, 24)),
                             command=lambda c=c, cv=cv: self.quick_filter(c, "only", cv))
        if same.index("end") is not None:
            m.add_cascade(label="Show rows with the same", menu=same)
        m.add_command(label="Filter %s…" % name, accelerator="Alt+Down",
                      command=lambda: self.open_filter_popover(col))
        if self.active_filters():
            m.add_command(label="Clear all filters", command=self.clear_filters)

    def _filter_to_selected(self, col):
        """'Filter to these values': the values of the selected rows in column col (IN)."""
        if self._sel is None:
            return None
        lo, hi = self._sel
        cap = limits.get("grid_copy_rows")
        if hi - lo + 1 > cap:
            hi = lo + cap - 1
            self.notice = ("filtered to the values of the first %s selected rows (raise %s)"
                           % (format(cap, ","), limits.hint("grid_copy_rows")))
            self._view_key = None
        rows = self._fetch_sync(lo, hi)
        vals = [values[col] for values, _flags in rows if col < len(values)]
        return self.quick_filter(col, "values", values=vals)

    def build_header_menu(self, c):
        if self._menu is not None:
            self._menu.destroy()
        m = self._menu = tk.Menu(self, tearoff=0)
        m.add_command(label="Sort ascending", command=lambda: self.sort_by(c, False))
        m.add_command(label="Sort descending", command=lambda: self.sort_by(c, True))
        m.add_command(label="Autosize column", command=lambda: self.autosize_column(c))
        m.add_separator()
        if self.filterable(c) and self._source is not None:
            m.add_command(label="Filter…", command=lambda: self.open_filter_popover(c))
            if self._fvars.get(c) is not None and self._fvars[c].get().strip():
                m.add_command(label="Clear filter", command=lambda: self.set_filter_text(c, ""))
        m.inline_var = tk.BooleanVar(m, value=self._inline)
        m.add_checkbutton(label="Filter row under the headers", variable=m.inline_var,
                          command=lambda: self.set_inline_filters(m.inline_var.get()))
        m.add_separator()
        if c >= self._frozen:           # the frozen row-id column cannot move or hide
            i = self._col_order.index(c) if c in self._col_order else -1
            last = len(self._col_order) - 1
            m.add_command(label="Move left", command=lambda: self.move_column(c, -1),
                          state="normal" if i > 0 else "disabled")
            m.add_command(label="Move right", command=lambda: self.move_column(c, +1),
                          state="normal" if 0 <= i < last else "disabled")
            m.add_command(label="Hide column", command=lambda: self.hide_column(c))
        m.add_command(label="Column chooser\u2026", command=self.column_chooser)
        m.add_separator()
        m.wrap_var = tk.BooleanVar(m, value=self._wrap)
        m.add_checkbutton(label="Wrap cell text", variable=m.wrap_var,
                          command=lambda: self.set_wrap(m.wrap_var.get()))
        if self.on_header_menu is not None:
            self.on_header_menu(m, c)
        tidy_menu(m)
        return m

    # -- keyboard ---------------------------------------------------------------------------------
    def _key_move(self, drow, dcol, extend=False):
        if self._cur is None:
            return
        row, col = self._cur
        if dcol:
            shown = self.displayed_columns()
            i = shown.index(col) if col in shown else 0
            col = shown[max(0, min(len(shown) - 1, i + dcol))]
        self.set_current_cell(row + drow, col, extend=extend)

    def _key_jump(self, row, col):
        if self._cur is None:
            return
        r, c = self._cur
        if row is not None:
            r = 0 if row == 0 else max(0, self._total - 1)
        if col is not None:
            shown = self.displayed_columns()
            if shown:
                c = shown[min(self._frozen, len(shown) - 1)] if col == 0 else shown[-1]
        self.set_current_cell(r, c)

    # -- inspector --------------------------------------------------------------------------------
    def _build_inspector(self):
        f = self._insp = tk.Frame(self._pane, bg=C["bg"], width=340)
        self._insp_title = tk.Label(f, text="Row panel", bg=C["bg"], fg=C["text"],
                                    font=self._fonts[2], anchor="w")
        self._insp_title.pack(fill="x", padx=6, pady=(4, 2))
        self._insp_flags = tk.Label(f, text="", bg=C["bg"], fg=C["red"], anchor="w",
                                    justify="left", wraplength=320, font=self._fonts[3])
        tf = self._insp_body = tk.Frame(f, bg=C["bg"])
        tf.pack(fill="both", expand=True)
        cols = ("type", "value", "decoded") if self.describe else ("type", "value")
        t = self._insp_tree = ttk.Treeview(tf, columns=cols, show="tree headings",
                                           selectmode="browse")
        t.heading("#0", text="Column")
        t.heading("type", text="Type")
        t.heading("value", text="Value")
        t.column("#0", width=110, stretch=False)
        t.column("type", width=62, stretch=False)
        t.column("value", width=220, stretch=True)
        if self.describe:
            t.heading("decoded", text="Decoded")
            t.column("decoded", width=160, stretch=False)
        ysb = ttk.Scrollbar(tf, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.bind("<Double-1>", self._insp_open)
        t.bind("<Control-c>", self._insp_copy)
        self._insp_key = None
        self._insp_values = []

    def _place_inspector_sash(self):
        try:
            total = self._pane.winfo_width()
            if total > 500:
                self._pane.sashpos(0, total - 360)
        except tk.TclError:
            pass

    def _refresh_inspector(self):
        row = self._cur[0] if self._cur is not None else None
        d = self.row_data(row) if row is not None else None
        key = (self._gen, row, d is not None, len(self._cols))
        if key == self._insp_key:
            return
        self._insp_key = key
        t = self._insp_tree
        t.delete(*t.get_children())
        self._insp_values = []
        if d is None:
            loading = row is not None and row < self._total and self._source is not None
            self._insp_title.configure(text="Row panel" + (" (loading\u2026)" if loading else ""))
            self._insp_flags.pack_forget()
            return
        values, flags = d
        self._insp_title.configure(text="Row panel \u2014 row %s" % format(row + 1, ","))
        notes = [ROW_FLAGS[f][1] for f in sorted(flags or ()) if f in ROW_FLAGS]
        if notes:
            self._insp_flags.configure(text="\n".join("\u26a0 " + n for n in notes))
            self._insp_flags.pack(fill="x", padx=6, before=self._insp_body)
        else:
            self._insp_flags.pack_forget()
        for c, name in enumerate(self._cols):
            v = values[c] if c < len(values) else None
            formatted = None
            if c < self._frozen:
                typ, shown = "locator", str(v)
            else:
                typ = value_type(v) + (" (invalid)" if isinstance(v, InvalidText) else "")
                shown = cell_text(v, limits.get("cell_tip_chars"))[0]
                formatted = self._formatted(v, c)
            if formatted is not None and not self.describe:
                shown = "%s  (%s)" % (shown, formatted)
            vals = [typ, shown]
            if self.describe:
                try:
                    vals.append(formatted or self.describe(v) or "")
                except Exception:       # noqa: BLE001 - decoding is a courtesy
                    vals.append("")
            t.insert("", "end", text=name, values=vals)
            self._insp_values.append(v)

    def _insp_index(self):
        sel = self._insp_tree.selection()
        return self._insp_tree.index(sel[0]) if sel else None

    def _insp_open(self, _e=None):
        i = self._insp_index()
        if i is None or i >= len(self._insp_values):
            return
        v = self._insp_values[i]
        if isinstance(v, bytes) and not isinstance(v, InvalidText) and self.on_open_blob:
            self.on_open_blob(self._cur[0], self._cols[i], v)

    def _insp_copy(self, _e=None):
        i = self._insp_index()
        if i is not None and i < len(self._insp_values):
            self.clipboard_clear()
            self.clipboard_append(plain_text(self._insp_values[i]))
        return "break"


class ColumnChooser(tk.Toplevel):
    """Show or hide the columns of a DataGrid: a searchable checkbox list."""

    def __init__(self, grid):
        tk.Toplevel.__init__(self, grid)
        self.grid_widget = grid
        self.title("Columns")
        from widgets import fit_geometry
        fit_geometry(self, 320, 460)
        self.configure(bg=C["bg"])
        self.transient(grid.winfo_toplevel())
        top = tk.Frame(self, bg=C["bg"])
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.search = SearchBox(top, placeholder="Find a column…", delay=0, find_button=False,
                                primary=True, width=20, count_below=True,
                                on_change=lambda t: self.rebuild(),
                                on_next=lambda forward: self._next(forward))
        self.search.pack(side="left", fill="x", expand=True)
        self.search_var = self.search.var
        self.search.entry.focus_set()
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=8, pady=8, side="bottom")
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(body, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))
        ttk.Button(bar, text="Show all", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Hide all", command=lambda: self.set_all(False)).pack(side="left",
                                                                                  padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.summary = tk.Label(self, text="", bg=C["bg"], fg=C["text2"], anchor="w")
        self.summary.pack(fill="x", padx=8, pady=(0, 6), side="bottom", after=bar)
        self.rebuild()

    def _next(self, forward=True):
        items = list(self.tree.get_children())
        if not items:
            return
        sel = self.tree.selection()
        i = items.index(sel[0]) if sel and sel[0] in items else -1
        i = (i + (1 if forward else -1)) % len(items) if i >= 0 else 0
        self.tree.selection_set(items[i])
        self.tree.focus(items[i])
        self.tree.see(items[i])

    def listed(self):
        """Column indices matching the search (the frozen column is never listed)."""
        g = self.grid_widget
        q = self.search_var.get().strip().lower()
        return [c for c in range(g._frozen, len(g._cols)) if q in g._cols[c].lower()]

    def rebuild(self):
        g = self.grid_widget
        self.tree.delete(*self.tree.get_children())
        for c in self.listed():
            mark = "\u2610" if c in g._hidden else "\u2611"
            self.tree.insert("", "end", iid=str(c), text="%s  %s" % (mark, g._cols[c]))
        shown = len(g._cols) - g._frozen - len(g._hidden)
        self.summary.configure(text="%d of %d columns shown" % (shown, len(g._cols) - g._frozen))
        self.search.set_count(len(self.tree.get_children()), len(g._cols) - g._frozen,
                              "column", "columns")

    def _on_click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self._toggle(iid)
            return "break"

    def _toggle(self, iid):
        if not iid:
            return
        g, c = self.grid_widget, int(iid)
        if c in g._hidden:
            g.show_column(c)
        else:
            g.hide_column(c)
        self.rebuild()
        if self.tree.exists(iid):
            self.tree.focus(iid)
            self.tree.selection_set(iid)

    def set_all(self, show):
        g = self.grid_widget
        for c in self.listed():
            if show:
                g.show_column(c)
            else:
                g.hide_column(c)
        self.rebuild()
