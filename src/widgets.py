"""Reusable UI widgets for SQLite GUI Analyzer."""

import sys
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

from constants import C
from tokens import COLOR as K, FONT as F


def enable_dpi_awareness():
    """On Windows, draw at the display's real resolution (125-200% scaling) instead of a
    blurred bitmap stretch: Tk then sizes the fonts (in points) for the real DPI. Only this
    process is affected; any failure keeps the default."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # system DPI aware
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:                   # noqa: BLE001 - cosmetic, never stops the start
        return False


def cancel_all_afters(widget):
    """Cancel every after() timer still pending in the widget's Tcl interpreter. Called just
    before a main window is destroyed: a timer left behind would call a Tcl command that the
    destroy deleted ('invalid command name ..._poll' on stderr)."""
    try:
        ids = widget.tk.splitlist(widget.tk.call("after", "info"))
    except tk.TclError:
        return 0
    n = 0
    for aid in ids:
        try:
            # only the timer: its command belongs to the widget that set it and goes with it
            widget.tk.call("after", "cancel", aid)
            n += 1
        except tk.TclError:
            pass
    return n


# ── tooltip helper ────────────────────────────────────────────────────────
class ToolTip:
    """Lightweight tooltip for any widget."""
    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._hide()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=self.text, bg=K["tip"], fg=K["tip_text"],
                       font=F["small"], padx=8, pady=4, relief="flat", bd=0,
                       wraplength=320, justify="left")
        lbl.pack()

    def _hide(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ── treeview cell tooltip ────────────────────────────────────────────────
class TreeviewTooltip:
    """Hover tooltip for Treeview cells — shows full cell text when truncated."""
    def __init__(self, tree, delay=400):
        self.tree = tree
        self.delay = delay
        self._tip = None
        self._after_id = None
        self._last_cell = (None, None)
        self._font = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<ButtonPress>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _on_motion(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        cell = (row, col)
        if cell == self._last_cell:
            return
        self._last_cell = cell
        self._hide()
        if row and col:
            self._after_id = self.tree.after(self.delay, lambda: self._show(row, col, event))

    def _show(self, row, col, event):
        if not self.tree.winfo_exists():
            return
        try:
            col_idx = int(col.replace("#", "")) - 1
            values = self.tree.item(row, "values")
            if col_idx < 0 or col_idx >= len(values):
                return
            text = str(values[col_idx])
            if not text or len(text) < 10:
                return
            # Measure actual text width using font
            if not self._font:
                self._font = tkfont.nametofont("TkDefaultFont")
            col_id = self.tree.cget("columns")[col_idx] if self.tree.cget("columns") else col
            col_width = self.tree.column(col_id, "width")
            text_px = self._font.measure(text)
            if text_px <= col_width - 20:
                return  # fits — no tooltip needed
        except Exception:
            return
        x = self.tree.winfo_rootx() + event.x + 15
        y = self.tree.winfo_rooty() + event.y + 20
        self._tip = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=text, bg=K["tip"], fg=K["tip_text"],
                       font=F["mono"], padx=8, pady=5, relief="flat", bd=0,
                       wraplength=500, justify="left")
        lbl.pack()
        # Keep tooltip within screen bounds
        tw.update_idletasks()
        sw = tw.winfo_screenwidth()
        sh = tw.winfo_screenheight()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        if x + tw_w > sw - 10:
            x = sw - tw_w - 10
        if y + tw_h > sh - 10:
            y = event.y_root - tw_h - 5
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._after_id:
            self.tree.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None
        self._last_cell = (None, None)


# ── layouts that never clip a control ────────────────────────────────────
def _exists(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def menu_button(parent, text, postcommand=None, **kw):
    """A ttk.Button that posts a drop-down tk.Menu under it, returned as
    (button, menu). Unlike ttk.Menubutton it draws no second, native indicator
    arrow next to the arrow already in the text ("Export \u25be")."""
    btn = ttk.Button(parent, text=text, **kw)
    menu = tk.Menu(btn, tearoff=0, postcommand=postcommand)

    def post():
        try:
            menu.tk_popup(btn.winfo_rootx(),
                          btn.winfo_rooty() + btn.winfo_height())
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    btn.configure(command=post)
    return btn, menu


def _text_changed(args, kw):

    """True when a configure-style call sets the text option (any form)."""
    if "text" in kw:
        return True
    args = list(args)
    if len(args) == 1 and isinstance(args[0], dict):
        return "text" in args[0]
    return any(args[i] in ("text", "-text")
               for i in range(0, len(args) - 1, 2))


class FlowFrame(ttk.Frame):
    """A toolbar row that lays its items out left to right and starts a new line when the next
    item would not fit, so no button, box or label is cut at a narrow window.

    Items are added in order with add(widget, stretch=False, gap=4): the widget must be a child
    of the FlowFrame. A 'stretch' item (an entry, a status text) takes the room left on its
    line. show(widget, False) takes an item out of the flow (and show(widget) puts it back in
    its place). A 'break' item (add_break()) always starts a new line.
    """

    def __init__(self, master, vgap=2, style=None, **kw):
        if style:
            kw["style"] = style
        ttk.Frame.__init__(self, master, **kw)
        self.vgap = vgap
        self._items = []                # [widget, gap, stretch, visible, min_width]
        self._after = None
        self._laying = False
        self.bind("<Configure>", self._schedule, add="+")

    def add(self, widget, stretch=False, gap=4, visible=True, min_width=None, before=None):
        item = [widget, gap, stretch, visible, min_width]
        if before is not None:
            i = next((k for k, it in enumerate(self._items) if it[0] is before), len(self._items))
            self._items.insert(i, item)
        else:
            self._items.append(item)
        if widget != "break":
            widget.bind("<Configure>", self._schedule, add="+")
            self._watch_text(widget)
        self._schedule()
        return widget

    def _watch_text(self, widget):
        """A text change does not fire <Configure> on a placed item (its size is
        fixed), so a longer text would be cut: re-lay-out after any configure
        that sets the text."""
        for name in ("configure", "config"):
            orig = getattr(widget, name, None)
            if orig is None or getattr(orig, "_flow_wrapped", False):
                continue

            def wrapper(*a, _orig=orig, **kw):
                result = _orig(*a, **kw)
                try:
                    if _text_changed(a, kw):
                        self._schedule()
                except tk.TclError:
                    pass
                return result

            wrapper._flow_wrapped = True
            try:
                setattr(widget, name, wrapper)
            except (AttributeError, tk.TclError):
                pass

    def add_break(self):
        self._items.append(["break", 0, False, True, None])

    def clear(self):
        """Destroy every item (a row rebuilt from scratch, e.g. the status chips)."""
        for it in self._items:
            if it[0] != "break":
                try:
                    it[0].destroy()
                except tk.TclError:
                    pass
        self._items = []
        self._schedule()

    def show(self, widget, visible=True):
        for it in self._items:
            if it[0] is widget:
                if it[3] != bool(visible):
                    it[3] = bool(visible)
                    if not visible:
                        widget.place_forget()
                    self._schedule()
                return

    def shown(self, widget):
        return any(it[0] is widget and it[3] for it in self._items)

    def items(self):
        return [it[0] for it in self._items if it[0] != "break"]

    def _schedule(self, _event=None):
        if self._after is None and not self._laying:
            try:
                self._after = self.after_idle(self.relayout)
            except tk.TclError:
                self._after = None

    def destroy(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        ttk.Frame.destroy(self)

    def relayout(self):
        """Place the items for the current width (also called by tests)."""
        self._after = None
        if not self.winfo_exists():
            return
        width = self.winfo_width()
        if width <= 1:
            width = max(self.winfo_reqwidth(), 1)
        # an item destroyed from outside leaves the flow
        self._items = [it for it in self._items
                       if it[0] == "break" or _exists(it[0])]
        lines, line, x = [], [], 0
        for w, gap, stretch, visible, min_w in self._items:
            if w == "break":
                if line:
                    lines.append(line)
                line, x = [], 0
                continue
            if not visible:
                continue
            rw = max(w.winfo_reqwidth(), min_w or 0)
            need = rw + (gap if line else 0)
            if line and x + need > width:
                lines.append(line)
                line, x, need = [], 0, rw
            line.append([w, x + (need - rw), rw, stretch])
            x += need
        if line:
            lines.append(line)
        self._laying = True
        try:
            y = 0
            for ln in lines:
                h = max(w.winfo_reqheight() for w, _x, _rw, _s in ln)
                used = ln[-1][1] + ln[-1][2]
                extra = max(0, width - used)
                stretchers = [it for it in ln if it[3]]
                shift = 0
                for it in ln:
                    w, x0, rw, stretch = it
                    grow = extra // len(stretchers) if stretch and stretchers else 0
                    wd = min(rw + grow, max(1, width - x0 - shift))
                    w.place(in_=self, x=x0 + shift, y=y + (h - w.winfo_reqheight()) // 2,
                             width=wd)
                    shift += grow
                y += h + self.vgap
            total = max(1, y - self.vgap if lines else 1)
            try:
                now = int(float(str(self.cget("height")) or 0))   # a Tcl_Obj on Tk 8.6
            except (tk.TclError, ValueError):
                now = -1
            if now != total:
                self.configure(height=total)
        finally:
            self._laying = False

    def line_count(self):
        """Lines the items take now (tests: a narrow window wraps the row)."""
        ys = set()
        for w in self.items():
            if self.shown(w) and w.winfo_manager() == "place":
                ys.add(int(w.place_info().get("y", 0)))
        return len(ys)


class ElideLabel(ttk.Label):
    """A one-line label that shortens its text in the middle ('C:\\Users\\…\\msgstore.db  | ...')
    when the room is too small, never clipping it at the end; the full text is its tooltip."""

    def __init__(self, master, text="", **kw):
        ttk.Label.__init__(self, master, **kw)
        self._full = ""
        self._tip = ToolTip(self, "")
        self.bind("<Configure>", lambda e: self._fit(), add="+")
        self.set_text(text)

    def set_text(self, text):
        self._full = str(text)
        self._tip.text = self._full
        self._fit()

    def full_text(self):
        return self._full

    def configure(self, cnf=None, **kw):
        """As ttk.Label.configure, with text= going through set_text (shortened to fit)."""
        if "text" in kw:
            self.set_text(kw.pop("text"))
            if not kw and not cnf:
                return None
        return ttk.Label.configure(self, cnf, **kw)

    config = configure

    def _fit(self):
        # a label whose width follows its own text could shorten, grow and shorten again
        # without end (each change is a new <Configure>): past 40 fits in a second the text
        # stays as it is until the next second, so the event loop is never kept busy
        import time
        now = time.monotonic()
        win = getattr(self, "_fit_window", None)
        if win is None or now - win[0] > 1.0:
            self._fit_window = win = [now, 0]
        win[1] += 1
        if win[1] > 40:
            return
        text = self._full
        try:
            font = tkfont.nametofont(str(self.cget("font"))) if self.cget("font") else \
                tkfont.nametofont("TkDefaultFont")
        except tk.TclError:
            font = tkfont.Font(font=self.cget("font"))
        avail = self.winfo_width() - 6
        if avail > 20 and font.measure(text) > avail:
            lo, hi = 1, len(text)
            best = "…"
            while lo <= hi:
                keep = (lo + hi) // 2
                head = (keep + 1) // 2
                cand = text[:head] + "…" + text[len(text) - (keep - head):]
                if font.measure(cand) <= avail:
                    best, lo = cand, keep + 1
                else:
                    hi = keep - 1
            text = best
        if str(self.cget("text")) != text:
            ttk.Label.configure(self, text=text)


# ── one search field for every list ─────────────────────────────────────────
class SearchBox(ttk.Frame):
    """The search field of a list, the same everywhere: grey placeholder text while empty,
    live filtering as you type (on_change(text) after `delay` ms of quiet), a × button that
    clears it, a Find button and Enter (Shift+Enter: back) that go to the next match
    (on_next(forward)), Escape that clears it, and 'N of M tables' / 'No table matches
    “xyz”' next to it (set_count). Ctrl+F in a window focuses its search field
    (focus_search_in)."""

    _boxes = []                 # every SearchBox alive (Ctrl+F looks for the one in view)

    def __init__(self, master, placeholder="Search…", on_change=None, on_next=None,
                 delay=200, width=26, tooltip=None, find_button=True, primary=False,
                 count_below=False, **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.on_change, self.on_next, self.delay = on_change, on_next, delay
        self.primary = primary
        self.var = tk.StringVar()
        self._after = None
        self._last = ""
        # packed right to left, the entry last: a narrow room shrinks the entry, never the
        # buttons
        self.count_label = ttk.Label(self, text="", style="M.TLabel")
        if count_below:                 # a narrow place: the count on its own line
            self.count_label.configure(wraplength=max(120, width * 7))
            self.count_label.pack(side="bottom", anchor="w")
        else:
            self.count_label.pack(side="right", padx=(6, 0))
        self.find_btn = ttk.Button(self, text="Find", style="Sm.TButton",
                                   command=lambda: self.next(True))
        if find_button:
            self.find_btn.pack(side="right", padx=(2, 0))
        self._before_clear = self.find_btn if find_button else self.count_label
        self.clear_btn = ttk.Button(self, text="×", width=2, style="Sm.TButton",
                                    command=self.clear)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        self.placeholder = placeholder_label(self.entry, placeholder, F["body"])
        self.entry.bind("<Return>", lambda e: (self.next(True), "break")[1])
        self.entry.bind("<Shift-Return>", lambda e: (self.next(False), "break")[1])
        self.entry.bind("<F3>", lambda e: (self.next(True), "break")[1])
        self.entry.bind("<Escape>", lambda e: self.clear())
        self.var.trace_add("write", lambda *_a: self._typed())
        tip = tooltip or ("Type to filter as you go; Enter or Find goes to the next match, "
                          "Shift+Enter to the previous one; Escape or × clears it.")
        ToolTip(self.entry, tip)
        ToolTip(self.clear_btn, "Clear the search")
        ToolTip(self.find_btn, "Go to the next match (Enter)")
        self._placeholder_shown = None
        self._update_placeholder()
        SearchBox._boxes.append(self)

    def destroy(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        try:
            SearchBox._boxes.remove(self)
        except ValueError:
            pass
        ttk.Frame.destroy(self)

    def get(self):
        """The words searched for ('' when empty), stripped."""
        return self.var.get().strip()

    def set(self, text, now=True):
        self.var.set(text)
        if now:
            self.flush()

    def clear(self):
        if self.var.get():
            self.var.set("")
        self.flush()
        self.entry.focus_set()

    def _update_placeholder(self):
        empty = not self.var.get()
        if empty != self._placeholder_shown:
            self._placeholder_shown = empty
            if empty:
                self.placeholder.place(x=4, rely=0.5, anchor="w", relwidth=1.0, width=-10)
                self.clear_btn.pack_forget()
            else:
                self.placeholder.place_forget()
                self.clear_btn.pack(side="right", padx=(2, 0), after=self._before_clear)

    def placeholder_visible(self):
        return bool(self._placeholder_shown)

    def _typed(self):
        self._update_placeholder()
        if not self.delay:              # a short list: filtered at each key
            self.flush()
            return
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
        try:
            self._after = self.after(self.delay, self.flush)
        except tk.TclError:
            self._after = None

    def flush(self):
        """Apply what is typed now (instead of waiting for the typing to pause)."""
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        self._update_placeholder()
        text = self.get()
        if text != self._last:
            self._last = text
            if self.on_change is not None:
                self.on_change(text)

    def next(self, forward=True):
        self.flush()
        if self.on_next is not None and self.get():
            self.on_next(forward)

    def set_count(self, n, total, noun="rows", nouns=None, what=None):
        """'3 of 88 tables match' while searching; 'No table matches “xyz”' when nothing
        does; '' with no search."""
        term = self.get()
        nouns = nouns or noun
        if not term:
            text = ""
        elif n:
            text = "%s of %s %s match" % (format(n, ","), format(total, ","),
                                          nouns if total != 1 else noun)
        else:
            text = "No %s matches “%s”" % (what or noun, term)
        self.count_label.configure(text=text,
                                   foreground=C["red"] if term and not n else C["text2"])
        return text

    def set_status(self, text, error=False):
        self.count_label.configure(text=text, foreground=C["red"] if error else C["text2"])

    def count_text(self):
        return str(self.count_label.cget("text"))

    def focus_search(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")


def placeholder_label(entry, text, font):
    """The grey hint inside an empty entry: shortened in the middle when the entry is
    narrower than it (never drawn past the entry); a click on it focuses the entry."""
    lbl = ElideLabel(entry, text=text, foreground=K["placeholder"], background=K["card"],
                     font=font, padding=0, cursor="xterm", anchor="w")
    lbl.bind("<Button-1>", lambda e: entry.focus_set())
    return lbl


def add_placeholder(entry, var, text, font=None):
    """Grey text inside an empty Entry (clicking it focuses the entry); gone while the entry
    holds text. Returns the label (its text is the placeholder)."""
    lbl = placeholder_label(entry, text, font if font is not None else F["small"])

    def update(*_a):
        try:
            if var.get():
                lbl.place_forget()
            else:
                lbl.place(x=3, rely=0.5, anchor="w", relwidth=1.0, width=-8)
        except tk.TclError:
            pass
    var.trace_add("write", update)
    update()
    entry.placeholder = lbl
    return lbl


def release_variables(obj):
    """Let go of the Tk variables an object keeps in its attributes, now, on the Tk thread.

    A tk.Variable unsets itself in Tcl when Python frees it. A window freed by a worker
    thread (the last job it ran still held it) would free its variables there, and Tcl may
    only be called from the Tk thread ('main thread is not in main loop'). Released here,
    the later free does nothing."""
    found = []
    for value in list(vars(obj).values()):
        if isinstance(value, dict):             # e.g. {name: StringVar}
            found.extend(value.values())
        elif isinstance(value, (list, tuple)):
            found.extend(value)
        else:
            found.append(value)
    for value in found:
        if isinstance(value, tk.Variable) and getattr(value, "_tk", None) is not None:
            try:
                tk.Variable.__del__(value)
            except (tk.TclError, RuntimeError):
                pass
            value._tk = None


TIP_LINES = 30          # lines a tooltip lists before '… and N more'


def cut_list(lines, most=TIP_LINES):
    """Lines for a tooltip, one per line: the first `most`, then '… and N more' (never cut
    without saying so)."""
    lines = [str(x) for x in lines]
    if len(lines) <= most:
        return "\n".join(lines)
    return "\n".join(lines[:most] + ["… and %s more" % format(len(lines) - most, ",")])


def next_line(tree, forward=True):
    """Select the next (previous) line of a flat list, wrapping; returns its iid."""
    items = list(tree.get_children())
    if not items:
        return None
    sel = tree.selection()
    i = items.index(sel[0]) if sel and sel[0] in items else -1
    i = (i + (1 if forward else -1)) % len(items) if i >= 0 else (0 if forward else
                                                                   len(items) - 1)
    tree.selection_set(items[i])
    tree.see(items[i])
    return items[i]


class TreeFilter(object):
    """A SearchBox filtering the lines of a flat Treeview: the lines holding every word stay
    (in their order), the others are set aside and come back when the words change. Call
    snapshot() after refilling the list (auto: lines inserted later are filtered by
    themselves, and emptying the list forgets the lines set aside). Enter selects the next line
    listed."""

    def __init__(self, tree, box, noun="line", nouns=None, what=None, auto=True):
        self.tree, self.box = tree, box
        self.noun, self.nouns, self.what = noun, nouns or noun + "s", what
        self._order = []
        self._after = None
        box.on_change = lambda _t: self.apply()
        box.on_next = lambda forward: next_line(tree, forward)
        if auto:
            insert, delete = tree.insert, tree.delete

            def ins(*a, **k):
                iid = insert(*a, **k)
                self._later()
                return iid

            def dele(*items):
                delete(*items)
                if not tree.get_children():     # the list was emptied: a new filling
                    self._forget()
                self._later()
            tree.insert, tree.delete = ins, dele
        self.snapshot()

    def _later(self):
        if self._after is None:
            try:
                self._after = self.tree.after_idle(self._run_later)
            except tk.TclError:
                self._after = None

    def _run_later(self):
        self._after = None
        try:
            if self.tree.winfo_exists():
                self.apply()
        except tk.TclError:
            pass

    def _forget(self):
        """Delete the lines set aside (they belong to a filling now gone)."""
        tree = self.tree
        gone = [i for i in self._order if tree.exists(i)]
        if gone:
            ttk.Treeview.delete(tree, *gone)
        self._order = []

    def snapshot(self):
        """The list was refilled: forget the lines of the last filling set aside."""
        tree = self.tree
        keep = set(tree.get_children())
        gone = [i for i in self._order if i not in keep and tree.exists(i)]
        if gone:
            ttk.Treeview.delete(tree, *gone)
        self._order = list(tree.get_children())
        self.apply()

    def apply(self):
        tree = self.tree
        words = self.box.get().lower().split()
        known = set(self._order)
        order = [i for i in self._order if tree.exists(i)] + \
            [i for i in tree.get_children() if i not in known]     # lines added since
        n = 0
        for iid in order:
            text = " ".join([str(tree.item(iid, "text"))] +
                            [str(v) for v in tree.item(iid, "values")]).lower()
            if all(w in text for w in words):
                tree.move(iid, "", n)
                n += 1
            else:
                tree.detach(iid)
        self._order = order
        self.box.set_count(n, len(order), self.noun, self.nouns, self.what)
        return n


class TextFind(object):
    """A SearchBox finding words in a Text: every place marked, the current one stronger,
    'N of M' beside it, Enter / Shift+Enter to the next / previous one."""

    def __init__(self, text, box):
        self.text, self.box = text, box
        self.hits, self.at = [], -1
        text.tag_configure("find_all", background=K["find_all"])
        text.tag_configure("find_at", background=C["yellow"])
        text.tag_raise("find_at")
        box.on_change = lambda _t: self.apply()
        box.on_next = self.next

    def apply(self):
        t, needle = self.text, self.box.get()
        t.tag_remove("find_all", "1.0", "end")
        t.tag_remove("find_at", "1.0", "end")
        self.hits, self.at = [], -1
        if not needle:
            self.box.set_status("")
            return 0
        pos = "1.0"
        while True:
            pos = t.search(needle, pos, stopindex="end", nocase=True)
            if not pos:
                break
            end = "%s+%dc" % (pos, len(needle))
            self.hits.append(pos)
            t.tag_add("find_all", pos, end)
            pos = end
        if not self.hits:
            self.box.set_status("Not found: “%s”" % needle, error=True)
            return 0
        self.next(True)
        return len(self.hits)

    def next(self, forward=True):
        if not self.hits:
            return None
        self.at = (self.at + (1 if forward else -1)) % len(self.hits)
        pos = self.hits[self.at]
        t = self.text
        t.tag_remove("find_at", "1.0", "end")
        t.tag_add("find_at", pos, "%s+%dc" % (pos, len(self.box.get())))
        t.see(pos)
        self.box.set_status("%d of %d" % (self.at + 1, len(self.hits)))
        return pos


def _in_view(widget, top):
    """True when the widget is laid out in its window up to `top`: every parent manages it,
    and inside a notebook its tab is the one selected."""
    w = widget
    while w is not None and w is not top:
        parent = w.master
        if isinstance(w, (tk.Toplevel, tk.Tk)):
            return True
        if not w.winfo_manager():
            return False
        if isinstance(parent, ttk.Notebook):
            try:
                if parent.select() != str(w):
                    return False
            except tk.TclError:
                return False
        w = parent
    return True


def search_boxes_in(widget, visible=True):
    """The SearchBoxes inside a widget (a window, a tab), those in view only by default."""
    out = []
    for b in list(SearchBox._boxes):
        try:
            if not b.winfo_exists():
                continue
            w = b
            inside = False
            while w is not None:
                if w is widget:
                    inside = True
                    break
                w = w.master
            if inside and (not visible or _in_view(b, widget)):
                out.append(b)
        except tk.TclError:
            continue
    return out


def focus_search_in(widget):
    """Ctrl+F: focus the search field in view in this window or tab (its primary one
    first); returns that SearchBox, or None when there is none."""
    boxes = search_boxes_in(widget)
    if not boxes:
        return None
    boxes.sort(key=lambda b: (not b.primary,))
    boxes[0].focus_search()
    return boxes[0]


def scaling_factor(widget):
    """The display scaling of a widget's Tk (1.0 at 100%, 1.25 at 125%...): Tk's own scaling
    is 1.33 pixels per point at 96 DPI."""
    try:
        return max(1.0, float(widget.tk.call("tk", "scaling")) / (96 / 72.0))
    except (tk.TclError, ValueError):
        return 1.0


def fit_geometry(win, width, height, margin=80, scale=True):
    """Give a window this size, or less on a screen too small for it (its title bar and
    buttons stay reachable)."""
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    except tk.TclError:
        sw = sh = 0
    f = scaling_factor(win) if scale else 1.0   # 125-200% display scaling: larger text
    width, height = int(width * f), int(height * f)
    if sw > 0 and sh > 0:
        width, height = min(width, max(400, sw - margin)), min(height, max(300, sh - margin))
    win.geometry("%dx%d" % (width, height))


def place_over(win, parent, third=True):
    """Put a small window over the middle of its parent window (a third of the way down), so
    it opens where the parent is."""
    try:
        win.update_idletasks()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - win.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - win.winfo_reqheight()) // (3 if third
                                                                                   else 2)
        win.geometry("+%d+%d" % (x, y))
    except tk.TclError:
        pass


def wrap_to_width(label, pad=8):
    """Let a label (packed or placed to fill its width) wrap its text at that width, so a long
    status line takes more lines instead of being cut."""
    def fit(e):
        w = max(80, e.width - pad)
        try:
            now = int(float(str(label.cget("wraplength") or 0)))
        except (tk.TclError, ValueError):
            now = 0
        if now != w:
            label.configure(wraplength=w)
    label.bind("<Configure>", fit, add="+")
    return label


# ── Theme setup ──────────────────────────────────────────────────────────
def setup_theme(root):
    """The ttk styles of the app, built from the design tokens (theme.py)."""
    import theme
    style = theme.apply(root)
    # the default fonts of classic Tk widgets and menus, from the tokens as well
    from tokens import FONT
    for name, key in (("TkDefaultFont", "body"), ("TkTextFont", "body"),
                      ("TkMenuFont", "body"), ("TkHeadingFont", "small_bold"),
                      ("TkFixedFont", "mono")):
        try:
            f = tkfont.nametofont(name)
            spec = FONT[key]
            f.configure(family=spec[0], size=spec[1],
                        weight="bold" if "bold" in spec[2:] else "normal")
        except tk.TclError:
            pass
    try:
        root.option_add("*Menu.activeBackground", C["tsel"])
        root.option_add("*Menu.activeForeground", C["text"])
        root.option_add("*Menu.background", C["bg"])
        root.option_add("*Menu.relief", "flat")
        root.option_add("*TCombobox*Listbox.font", FONT["body"])
        # classic-Tk widgets draw their focus/highlight ring in the platform
        # colour (black boxes on macOS): the app draws its own focus rings on
        # the ttk styles, so the classic default is switched off (an explicit
        # highlightthickness on a widget still wins over this default)
        root.option_add("*highlightThickness", 0)
    except tk.TclError:
        pass
    return style


def set_app_theme(root, name):
    """Switch the whole app between the light and dark theme, live: the tokens, the ttk
    styles, every classic widget, canvas item and text/tree tag. Returns the theme in
    effect ('light' or 'dark')."""
    import tokens
    import constants
    import theme
    old = dict(tokens.COLOR)
    name = tokens.set_theme(name)
    constants.refresh_colors()
    theme.apply(root)
    _recolor_tree(root, old, tokens.COLOR)
    try:                                            # defaults for menus made from now on
        root.option_add("*Menu.activeBackground", C["tsel"])
        root.option_add("*Menu.activeForeground", C["text"])
        root.option_add("*Menu.background", C["bg"])
        root.option_add("*TCombobox*Listbox.background", K["card"])
        root.option_add("*TCombobox*Listbox.foreground", K["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", K["selection"])
        root.option_add("*TCombobox*Listbox.selectForeground", K["text"])
        # classic widgets made from now on: the theme's text and field colours, never the
        # platform's black-on-white (a dark field with black text cannot be read)
        for cls in ("Text", "Listbox", "Entry", "Spinbox"):
            root.option_add("*%s.foreground" % cls, K["text"])
            root.option_add("*%s.background" % cls, K["card"])
            root.option_add("*%s.insertBackground" % cls, K["text"])
            root.option_add("*%s.selectBackground" % cls, K["selection"])
            root.option_add("*%s.selectForeground" % cls, K["text"])
        for cls in ("Label", "Checkbutton", "Radiobutton", "Button"):
            root.option_add("*%s.foreground" % cls, K["text"])
        root.option_add("*Toplevel.background", K["background"])
        root.option_add("*Frame.background", K["background"])
    except tk.TclError:
        pass
    return name


_WIDGET_COLOR_OPTS = ("background", "foreground", "activebackground", "activeforeground",
                      "selectbackground", "selectforeground", "highlightbackground",
                      "highlightcolor", "troughcolor", "insertbackground",
                      "disabledforeground", "readonlybackground")
_TAG_COLOR_OPTS = ("foreground", "background", "selectforeground", "selectbackground")
_ITEM_COLOR_HINTS = ("fill", "color", "outline", "ground")


from tokens import (PLATFORM_DEFAULT_FG as _DEFAULT_FG,  # noqa: E402
                    PLATFORM_DEFAULT_BG as _DEFAULT_BG,
                    PLATFORM_DEFAULT_SELECT as _DEFAULT_SELECT)
_FIELD_CLASSES = ("Text", "Listbox", "Entry", "Spinbox", "Canvas")


def _default_color(cls, opt, cur, new):
    """The theme's colour for an option still at the platform default, or None."""
    v = str(cur).lower()
    if opt in ("foreground", "insertbackground", "activeforeground", "selectforeground")             and v in _DEFAULT_FG:
        return new.get("text")
    if opt in ("background", "activebackground", "readonlybackground") and v in _DEFAULT_BG:
        return new.get("card" if cls in _FIELD_CLASSES else "background")
    if opt == "selectbackground" and v in _DEFAULT_SELECT:
        return new.get("selection")
    return None


def _recolor_tree(root, old, new):
    """Point every classic-Tk colour at the new theme: an option holding a token value of
    the old theme takes the same token of the new one. ttk widgets are style-driven
    (theme.apply rebuilt them); their text/tree tags are recolored too. Colours that are
    not tokens (row flags, per-database colours) are left alone."""
    old_to_key = {}
    for key, value in old.items():
        old_to_key.setdefault(str(value).lower(), key)
    # Several light tokens share a value; a bare colour cannot say which one it was. These
    # are the token a classic widget most likely meant (ttk-only tokens never reach here,
    # theme.apply() rebuilt those styles already).
    # Only when leaving the light theme: its preferred tokens are light values (the light
    # 'border' equals the dark 'muted_text', so applied when leaving dark it turned muted text
    # into the faint light border colour)
    from tokens import LIGHT_COLOR, DARK_COLOR
    if dict(old) == dict(LIGHT_COLOR):
        for key in ("border", "selection", "hover", "placeholder",
                    "danger_text", "success_text"):
            old_to_key[LIGHT_COLOR[key].lower()] = key
    elif dict(old) == dict(DARK_COLOR):
        for key in ("muted_text", "text", "card", "background", "primary", "selection",
                    "heading", "button_border"):
            old_to_key[DARK_COLOR[key].lower()] = key

    def remap(value):
        # str() also unwraps the Tcl_Obj a "tag configure" read returns on a Treeview
        key = old_to_key.get(str(value).lower())
        if key is not None:
            return new.get(key)
        return None

    def recolor_tag(w, tag, read, write):
        for opt in _TAG_COLOR_OPTS:
            try:
                cur = read(tag, opt)
            except tk.TclError:
                continue
            nv = remap(cur)
            if nv:
                try:
                    write(tag, opt, nv)
                except tk.TclError:
                    pass

    seen, stack = set(), [root]
    while stack:
        w = stack.pop()
        if id(w) in seen:
            continue
        seen.add(id(w))
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            continue
        cls = w.winfo_class()
        if cls == "Text":                            # tags keep their own colours
            try:
                tags = w.tag_names()
            except (tk.TclError, AttributeError):
                tags = ()
            for tag in tags:
                recolor_tag(w, tag,
                            lambda tag, opt: w.tag_cget(tag, opt),
                            lambda tag, opt, nv: w.tag_configure(tag, **{opt: nv}))
        elif cls == "Treeview":
            # Treeview has no tag_names()/tag_cget(); the tag subcommands do the job
            try:
                tags = w.tk.call(w, "tag", "names")
            except tk.TclError:
                tags = ()
            for tag in tags:
                recolor_tag(w, tag,
                            lambda tag, opt: w.tk.call(w, "tag", "configure", tag,
                                                       "-" + opt),
                            lambda tag, opt, nv: w.tag_configure(tag, **{opt: nv}))
        # NB: ttk widgets are NOT skipped. theme.apply() rebuilt their styles, but a ttk
        # widget may still carry a per-widget colour (e.g. a placeholder label placed over
        # an entry). cget() on those returns "" when unset, so only explicit colours remap.
        for opt in _WIDGET_COLOR_OPTS:
            try:
                cur = w.cget(opt)
            except tk.TclError:
                continue
            nv = remap(cur)
            if nv is None:
                # a colour left at the platform default (black text, a white or grey field)
                # follows the theme too: in dark it would be black text on a dark field
                nv = _default_color(cls, opt, cur, new)
            if nv:
                try:
                    w.configure(**{opt: nv})
                except tk.TclError:
                    pass
        if cls == "Canvas":                       # items drawn with token colours
            try:
                items = w.find_all()
            except tk.TclError:
                continue
            for item in items:
                try:
                    cfg = w.itemconfig(item)
                except tk.TclError:
                    continue
                for opt, spec in cfg.items():
                    if not any(h in opt for h in _ITEM_COLOR_HINTS):
                        continue
                    cur = spec[-1] if isinstance(spec, tuple) else spec
                    nv = remap(cur)
                    if nv:
                        try:
                            w.itemconfig(item, **{opt: nv})
                        except tk.TclError:
                            pass
