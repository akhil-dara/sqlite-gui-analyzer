"""A searchable dropdown: a drop-in replacement for ttk.Combobox, like the dropdowns of a
browser form.

SearchableCombobox(parent, textvariable=var, values=[...], state="readonly", width=28,
postcommand=fn) takes the ttk.Combobox options and methods (get, set, current, configure /
cget / [] for values, state, width, postcommand, textvariable, height, font) and sends
<<ComboboxSelected>> when the user picks a choice. The list it opens:

  * has a search field (readonly) or filters as you type (normal): exact, prefix, substring,
    word starts, then any subsequence ('msgdb' finds 'msgstore.db'), matched characters
    highlighted, the count ('12 of 480') below and 'No match for “xyz”' when nothing matches;
  * is a virtual list drawn on a Canvas (only the rows in view exist), so 50,000 values open
    at once;
  * lists the recent choices first (recent_key=..., at most the 'dropdown_recent' limit; the
    app keeps them with set_recent_store), can be grouped (groups=[(title, values), ...])
    and show a count per value (counts={value: n}) or a display text (labels={value: text}).

The matcher is plain Python: match(query, text) and filter_items(query, items).
"""

import heapq
import sys
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

from engine import limits
from tokens import COLOR, FONT, XS, S

ROW = 24                        # px, one row of the list
_MAX_POPUP = 560                # px, the widest the list grows to fit long values
_CHECK = "✓"               # the mark of the current value
_STYLE = "SearchableCombo.TEntry"

# ── the matcher (no Tk) ───────────────────────────────────────────────────────
EXACT, PREFIX, SUBSTRING, WORD_START, SUBSEQUENCE = range(5)


def _word_starts(text):
    """Indexes where a word starts: after a non-alphanumeric character, at a lower-to-upper
    case change (camelCase) and where letters and digits meet."""
    out = []
    prev = ""
    for i, ch in enumerate(text):
        if ch.isalnum() and (not prev or not prev.isalnum()
                             or (ch.isupper() and prev.islower())
                             or ch.isdigit() != prev.isdigit()):
            out.append(i)
        prev = ch
    return out


def _is_subsequence(q, low):
    it = iter(low)
    return all(c in it for c in q)


def _match(q, text, low):
    """match() with the query already lower-cased and the text's lower-case given."""
    if not q.strip():
        return (EXACT, [])
    if low == q:
        return (EXACT, list(range(len(text))))
    if low.startswith(q):
        return (PREFIX, list(range(len(q))))
    i = low.find(q)
    if i >= 0:
        # the occurrence at the start of a word when there is one
        j = i
        while j > 0 and low[j - 1].isalnum():
            j = low.find(q, j + 1)
            if j < 0:
                j = i
                break
        return (SUBSTRING, list(range(j, j + len(q))))
    compact = "".join(q.split())
    if not compact or not _is_subsequence(compact, low):
        return None
    starts = _word_starts(text)
    tokens = q.split()
    pos = []
    if len(tokens) > 1:                 # each word typed starts a later word of the text
        w = 0
        for tok in tokens:
            while w < len(starts) and not low.startswith(tok, starts[w]):
                w += 1
            if w == len(starts):
                pos = None
                break
            pos.extend(range(starts[w], starts[w] + len(tok)))
            w += 1
    else:                               # chunks of the query start later words ('mdb')
        k, w = 0, 0
        while k < len(compact) and w < len(starts):
            s = starts[w]
            n = 0
            while (k + n < len(compact) and s + n < len(low)
                   and low[s + n] == compact[k + n]):
                n += 1
            if n:
                pos.extend(range(s, s + n))
                k += n
            w += 1
        if k < len(compact):
            pos = None
    if pos:
        return (WORD_START, pos)
    pos = []
    at = 0
    for c in compact:
        at = low.find(c, at)
        pos.append(at)
        at += 1
    return (SUBSEQUENCE, pos)


def match(query, text):
    """How well text matches query, case-insensitive: (rank, positions) or None.

    rank: 0 exact, 1 prefix, 2 substring, 3 the query's words or pieces start words of the
    text, 4 the query's characters in order (a subsequence). positions: the indexes of the
    matched characters of text. An empty query matches everything: (0, [])."""
    text = text if isinstance(text, str) else str(text)
    return _match(str(query).lower(), text, text.lower())


def filter_items(query, items, key=None):
    """[(item, positions)] of the items matching query, the best rank first, the original
    order within a rank. key(item) gives the text matched (default str)."""
    q = str(query).lower()
    if not q.strip():
        return [(it, []) for it in items]
    buckets = ([], [], [], [], [])
    for it in items:
        t = key(it) if key is not None else (it if isinstance(it, str) else str(it))
        r = _match(q, t, t.lower())
        if r is not None:
            buckets[r[0]].append((it, r[1]))
    out = []
    for b in buckets:
        out.extend(b)
    return out


# ── recent choices ────────────────────────────────────────────────────────────
_recent = {}                                        # key -> [text, ...], newest first
_store = {"load": None, "save": None, "loaded": False}


def set_recent_store(load_fn=None, save_fn=None):
    """Where recent choices live between runs: load_fn() returns {key: [text, ...]} (or
    None), save_fn(data) is called with that dict after each choice. Without a store they
    are kept for this run only."""
    _store.update(load=load_fn, save=save_fn, loaded=False)
    _recent.clear()


def _ensure_loaded():
    if _store["loaded"]:
        return
    _store["loaded"] = True
    fn = _store["load"]
    data = None
    if fn is not None:
        try:
            data = fn()
        except Exception:               # noqa: BLE001 - a bad store: start empty
            data = None
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (list, tuple)):
                _recent[str(k)] = [str(x) for x in v]


def recent_choices(key):
    """The recent choices of a dropdown key, newest first (at most 'dropdown_recent')."""
    if not key:
        return []
    _ensure_loaded()
    return list(_recent.get(key, ()))[:max(0, limits.get("dropdown_recent"))]


def record_recent(key, text):
    """Note a choice of the dropdown key (newest first, capped by 'dropdown_recent')."""
    if not key:
        return
    _ensure_loaded()
    cap = max(0, limits.get("dropdown_recent"))
    text = str(text)
    lst = [t for t in _recent.get(key, ()) if t != text]
    lst.insert(0, text)
    _recent[key] = lst[:cap]
    fn = _store["save"]
    if fn is not None:
        try:
            fn(dict((k, list(v)) for k, v in _recent.items()))
        except Exception:               # noqa: BLE001 - never breaks a choice
            pass


def clear_recent(key=None):
    """Forget the recent choices of one key (all keys when None); not saved."""
    if key is None:
        _recent.clear()
    else:
        _recent.pop(key, None)


# ── the widget ────────────────────────────────────────────────────────────────
_OWN = ("values", "state", "width", "postcommand", "textvariable", "height", "font",
        "groups", "counts", "labels", "placeholder", "recent_key")


def _guarded(fn):
    """An event handler that does nothing once the widget is gone (and never raises a
    TclError of a widget destroyed under it)."""
    def wrapper(self, *a, **k):
        if self._dead:
            return None
        try:
            return fn(self, *a, **k)
        except tk.TclError:
            if self._dead or not _exists(self):
                return None
            raise
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _exists(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _fmt_count(n):
    try:
        return format(n, ",")
    except (TypeError, ValueError):
        return str(n)


def _lookup(mapping, text_map, value):
    if not mapping:
        return None
    try:
        if value in mapping:
            return mapping[value]
    except TypeError:                   # an unhashable value
        pass
    return text_map.get(str(value))


class SearchableCombobox(ttk.Frame):
    """A ttk.Combobox look-alike whose list can be searched (see the module doc).

    Options (constructor, configure, cget, [key]): values, state ('readonly' | 'normal' |
    'disabled'), width (characters), postcommand, textvariable, height (rows of the list),
    font, groups [(title, [values])], counts {value: n}, labels {value: text}, placeholder,
    recent_key; any other option goes to the ttk.Frame. Sends <<ComboboxSelected>> when the
    user picks a value."""

    def __init__(self, master=None, textvariable=None, values=(), state="normal", width=20,
                 postcommand=None, height=12, font=None, groups=None, counts=None,
                 labels=None, placeholder="", recent_key=None,
                 cycle_on_arrows=False, **kw):
        ttk.Frame.__init__(self, master, **kw)
        self._dead = False
        self._popup = None
        self._afters = set()
        self._sync = False              # set while this widget writes a variable itself
        self._var = None
        self._trace = None
        self._values = []
        self._groups = None             # [(title, [value index, ...])]
        self._counts, self._count_text = {}, {}
        self._labels, self._label_text = {}, {}
        self._state = "normal"
        self._height = 12
        self._postcommand = None
        self._font_spec = font
        self._placeholder_text = ""
        self.recent_key = None
        self._cycle_on_arrows = False
        self._shown = tk.StringVar(self)        # the text the entry shows
        self._orig, self._query, self._rows, self._nmatch = "", "", [], 0
        self._active, self._top, self._vis, self._cw = -1, 0, 1, 0
        self._canvas = self._search = self._svar = None
        self._click_line = self._tl_tag = None
        self._ensure_style()
        self.entry = ttk.Entry(self, textvariable=self._shown, width=width, style=_STYLE,
                               font=font or FONT["body"])
        self.entry.pack(fill="x", expand=True)
        self._arrow = tk.Canvas(self, width=16, height=16, highlightthickness=0, bd=0,
                                background=COLOR["card"], cursor="hand2", takefocus=0)
        self._arrow.place(in_=self.entry, relx=1.0, x=-4, rely=0.5, anchor="e")
        self._draw_arrow()
        self._ph = tk.Label(self.entry, text="", foreground=COLOR["placeholder"],
                            background=COLOR["card"], font=font or FONT["body"], bd=0,
                            padx=0, pady=0, anchor="w")
        self._shown.trace_add("write", self._on_shown_write)
        for w in (self.entry, self._ph):
            w.bind("<Button-1>", self._on_entry_click)
        self._arrow.bind("<Button-1>", self._on_arrow_click)
        self._arrow.bind("<Enter>", lambda e: self._draw_arrow(hover=True))
        self._arrow.bind("<Leave>", lambda e: self._draw_arrow())
        e = self.entry
        e.bind("<Key>", self._on_entry_key)
        for seq in ("<Down>", "<Up>", "<Prior>", "<Next>"):
            e.bind(seq, self._on_entry_nav)
        e.bind("<Alt-Down>", self._on_alt_down)
        e.bind("<space>", self._on_entry_space)
        e.bind("<Return>", self._on_entry_return)
        e.bind("<KP_Enter>", self._on_entry_return)
        e.bind("<Escape>", self._on_entry_escape)
        e.bind("<Tab>", self._on_entry_tab)
        e.bind("<FocusOut>", self._on_focus_out, add="+")
        self._set_var(textvariable)
        self._configure_own({"values": values, "groups": groups, "counts": counts,
                             "labels": labels, "state": state, "height": height,
                             "postcommand": postcommand, "placeholder": placeholder,
                             "recent_key": recent_key,
                             "cycle_on_arrows": cycle_on_arrows})

    # ── ttk.Combobox API ──────────────────────────────────────────────────────
    def get(self):
        """The current value as text (what the variable holds; in 'normal' state what is
        typed)."""
        return self._var.get()

    def set(self, value):
        self._var.set("" if value is None else str(value))

    def current(self, index=None):
        """Without index: the index of the current value in values, or -1. With index:
        make values[index] the current value."""
        if index is None:
            text = self.get()
            for i, v in enumerate(self._values):
                if (v if isinstance(v, str) else str(v)) == text:
                    return i
            return -1
        index = int(index)
        if not 0 <= index < len(self._values):
            raise tk.TclError("index %d out of range" % index)
        self.set(self._values[index])
        return None

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str) and not kw:         # configure("values"): one description
            if cnf in _OWN:
                return (cnf, cnf, cnf, "", self.cget(cnf))
            return ttk.Frame.configure(self, cnf)
        opts = dict(cnf or {})
        opts.update(kw)
        if not opts:
            out = ttk.Frame.configure(self) or {}
            for k in _OWN:
                out[k] = (k, k, k, "", self.cget(k))
            return out
        own = dict((k, opts.pop(k)) for k in list(opts) if k in _OWN)
        if own:
            self._configure_own(own)
        if opts:
            return ttk.Frame.configure(self, opts)
        return None

    config = configure

    def cget(self, key):
        if key == "values":
            return tuple(self._values)
        if key == "state":
            return self._state
        if key == "width":
            return int(str(self.entry.cget("width")))
        if key == "postcommand":
            return self._postcommand
        if key == "textvariable":
            return str(self._var)
        if key == "height":
            return self._height
        if key == "font":
            return self._font_spec
        if key == "groups":
            return None if self._groups is None else [
                (t, [self._values[i] for i in idx]) for t, idx in self._groups]
        if key == "counts":
            return dict(self._counts)
        if key == "labels":
            return dict(self._labels)
        if key == "placeholder":
            return self._placeholder_text
        if key == "recent_key":
            return self.recent_key
        return ttk.Frame.cget(self, key)

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.configure({key: value})

    def keys(self):
        out = list(ttk.Frame.keys(self))
        for k in _OWN:
            if k not in out:
                out.append(k)
        return out

    def focus_set(self):
        self.entry.focus_set()

    focus = focus_set

    def destroy(self):
        if not self._dead:
            try:
                self.close(focus=False)
            except tk.TclError:
                pass
            self._dead = True
            for aid in list(self._afters):
                try:
                    self.after_cancel(aid)
                except tk.TclError:
                    pass
            self._afters.clear()
            self._drop_trace()
        ttk.Frame.destroy(self)

    # ── options ───────────────────────────────────────────────────────────────
    def _configure_own(self, own):
        if "groups" in own and own["groups"] is not None:
            vals, groups = [], []
            for title, items in own["groups"]:
                items = list(items)
                groups.append((str(title), list(range(len(vals), len(vals) + len(items)))))
                vals.extend(items)
            self._values, self._groups = vals, groups
            own.pop("values", None)
        elif "groups" in own:
            self._groups = None
        if "values" in own:
            v = own["values"]
            if v is None:
                v = ()
            elif isinstance(v, str):
                v = self.tk.splitlist(v)
            self._values = list(v)
            self._groups = None
        if "counts" in own:
            self._counts = dict(own["counts"] or {})
            self._count_text = dict((str(k), n) for k, n in self._counts.items())
        if "labels" in own:
            self._labels = dict(own["labels"] or {})
            self._label_text = dict((str(k), t) for k, t in self._labels.items())
            self._show_value()
        if "textvariable" in own:
            self._set_var(own["textvariable"])
        if "width" in own:
            self.entry.configure(width=own["width"])
        if "height" in own:
            self._height = max(1, int(own["height"] or 12))
        if "postcommand" in own:
            self._postcommand = own["postcommand"] or None
        if "cycle_on_arrows" in own:
            self._cycle_on_arrows = bool(own["cycle_on_arrows"])
        if "font" in own:
            self._font_spec = own["font"]
            self.entry.configure(font=own["font"] or FONT["body"])
            self._ph.configure(font=own["font"] or FONT["body"])
        if "placeholder" in own:
            self._placeholder_text = str(own["placeholder"] or "")
            self._ph.configure(text=self._placeholder_text)
            self._update_placeholder()
        if "recent_key" in own:
            self.recent_key = own["recent_key"] or None
        if "state" in own:
            self._apply_state(str(own["state"] or "normal"))
        if self._popup is not None and ({"values", "groups", "counts", "labels"} & set(own)):
            self._prepare()
            self._refilter()

    def _ensure_style(self):
        style = ttk.Style(self)
        style.configure(_STYLE, padding=(6, 3, 24, 3),
                        fieldbackground=COLOR["card"], foreground=COLOR["text"],
                        background=COLOR["card"], insertcolor=COLOR["text"]) 
        style.map(_STYLE, fieldbackground=[("disabled", COLOR["muted"]),
                                           ("readonly", COLOR["card"])],
                  foreground=[("disabled", COLOR["disabled_text"])])

    def _apply_state(self, state):
        if state not in ("normal", "readonly", "disabled"):
            raise tk.TclError('bad state "%s": must be normal, readonly or disabled' % state)
        self._state = state
        self.entry.configure(state=state)
        bg = COLOR["muted"] if state == "disabled" else COLOR["card"]
        self._arrow.configure(background=bg, cursor="arrow" if state == "disabled" else "hand2")
        self._ph.configure(background=bg)
        self.entry.configure(cursor={"readonly": "hand2", "disabled": "arrow"}.get(state,
                                                                                    "xterm"))
        self._ph.configure(cursor="hand2" if state == "readonly" else "xterm")
        self._draw_arrow()
        if state == "disabled":
            self.close(focus=False)

    def _set_var(self, var):
        if var is not None and not isinstance(var, tk.Variable):
            var = tk.StringVar(self, name=str(var))
        if var is None:
            var = tk.StringVar(self)
        self._drop_trace()
        self._var = var
        self._trace = var.trace_add("write", self._on_var_write)
        self._show_value()

    def _drop_trace(self):
        if self._var is not None and self._trace is not None:
            try:
                self._var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
        self._trace = None

    # ── text shown <-> value ──────────────────────────────────────────────────
    def _label(self, value):
        lab = _lookup(self._labels, self._label_text, value)
        if lab is not None:
            return str(lab)
        return value if isinstance(value, str) else str(value)

    def _show_value(self):
        if self._var is None:
            return
        try:
            text = self._var.get()
        except tk.TclError:
            text = ""
        lab = self._label_text.get(text)
        shown = str(lab) if lab is not None else text
        if self._shown.get() != shown:
            self._sync = True
            try:
                self._shown.set(shown)
            finally:
                self._sync = False
        self._update_placeholder()

    def _on_var_write(self, *_a):
        if self._dead or self._sync:
            return
        try:
            self._show_value()
            if self._popup is not None:
                self._draw()
        except tk.TclError:
            pass

    def _on_shown_write(self, *_a):
        if self._dead:
            return
        try:
            self._update_placeholder()
            if self._sync:
                return
            # typed by the user ('normal' state): the typed text is the value
            text = self._shown.get()
            before = self._var.get()
            self._sync = True
            try:
                self._var.set(text)
            finally:
                self._sync = False
            if self._state == "normal":
                self._typed(text, before)
        except tk.TclError:
            pass

    def _update_placeholder(self):
        try:
            if self._placeholder_text and not self._shown.get():
                self._ph.place(x=7, rely=0.5, anchor="w", relwidth=1.0, width=-34)
                self._arrow.lift()
            else:
                self._ph.place_forget()
        except tk.TclError:
            pass

    def placeholder_visible(self):
        return bool(self._ph.winfo_manager())

    # ── the arrow ─────────────────────────────────────────────────────────────
    def _draw_arrow(self, hover=False):
        c = self._arrow
        c.delete("all")
        if self._state == "disabled":
            color = COLOR["disabled_text"]
        else:
            color = COLOR["primary"] if hover or self._popup is not None else \
                COLOR["muted_text"]
        if self._popup is not None:
            pts = (4, 10, 8, 6, 12, 10)             # open: ▴
        else:
            pts = (4, 6, 8, 10, 12, 6)              # closed: ▾
        c.create_line(*pts, fill=color, width=2, capstyle="round", joinstyle="round")

    # ── entry events ──────────────────────────────────────────────────────────
    @_guarded
    def _on_entry_click(self, event=None):
        if self._state == "disabled":
            return "break"
        if self._state == "readonly":
            self.entry.focus_set()
            self.toggle()
            return "break"
        if event is not None and event.widget is self._ph:
            self.entry.focus_set()
        return None

    @_guarded
    def _on_arrow_click(self, event=None):
        if self._state == "disabled":
            return "break"
        self.entry.focus_set()
        self.toggle()
        return "break"

    @_guarded
    def _on_entry_key(self, event):
        if self._state == "disabled":
            return "break" if event.char else None
        if self._state != "readonly":
            return None                 # 'normal': the entry edits, _on_shown_write filters
        ch = event.char
        if (ch and len(ch) == 1 and ch.isprintable() and ch != " "
                and not (event.state & 0x4) and not (event.state & 0x20000)):
            if self._popup is None:
                self.open(query=ch)
            return "break"
        return None

    @_guarded
    def _on_entry_space(self, event):
        if self._state == "disabled":
            return "break"
        if self._state == "readonly":
            if self._popup is None:
                self.open()
            return "break"
        return None

    @_guarded
    def _on_entry_nav(self, event):
        if self._state == "disabled":
            return "break"
        if self._popup is None:
            if event.keysym in ("Down", "Up") and self._cycle_on_arrows:
                self._cycle_value(1 if event.keysym == "Down" else -1)
                return "break"
            if event.keysym == "Down":
                self.open()
                return "break"
            return None
        return self._nav(event.keysym)

    def _cycle_value(self, step):
        """Move selection by step through values (DB Browser-style arrow cycling)."""
        if not self._values:
            return
        cur = self.get()
        try:
            idx = [str(v) for v in self._values].index(str(cur))
        except ValueError:
            idx = -1 if step > 0 else 0
        idx = (idx + step) % len(self._values)
        self.set(self._values[idx])
        self.event_generate("<<ComboboxSelected>>")

    @_guarded
    def _on_alt_down(self, event):
        if self._state != "disabled":
            self.toggle()
        return "break"

    @_guarded
    def _on_entry_return(self, event):
        if self._popup is None or self._state == "disabled":
            return None
        if self._active >= 0:
            self._choose_row(self._active)
            return "break"
        self.close()                    # 'normal': the typed text stays the value
        return None

    @_guarded
    def _on_entry_escape(self, event):
        if self._popup is None:
            return None
        self.close(restore=True)
        return "break"

    @_guarded
    def _on_entry_tab(self, event):
        if self._popup is None:
            return None
        if self._active >= 0:
            self._choose_row(self._active)
        else:
            self.close()
        return None                     # the usual traversal moves the focus on

    def _typed(self, text, before):
        if self._popup is None:
            self.open(query=text, _orig=before)
        else:
            self._query = text
            self._refilter()

    def _focus_now(self):
        try:
            return self.focus_get()
        except (KeyError, tk.TclError):
            return None

    # ── the popup ─────────────────────────────────────────────────────────────
    def is_open(self):
        return self._popup is not None

    def toggle(self):
        if self._popup is None:
            self.open()
        else:
            self.close()

    def open(self, query="", _orig=None):
        """Open the list (postcommand first). query: the search to start with."""
        if self._dead or self._popup is not None or self._state == "disabled":
            return
        if self._postcommand is not None:
            try:
                self._postcommand()
            except Exception:           # noqa: BLE001 - reported like any Tk callback
                self._report()
            if self._dead or self._popup is not None:
                return
        self._prepare()
        self._orig = self.get() if _orig is None else _orig
        self._query = ""
        self._top = 0
        self._active = -1
        self._rows, self._nmatch = self._build_rows("")
        vis = max(1, min(self._height, len(self._rows)))
        self._vis = vis
        readonly = self._state == "readonly"

        top = tk.Toplevel(self)
        self._popup = top
        top.withdraw()
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        top.configure(background=COLOR["border"])
        body = tk.Frame(top, background=COLOR["card"], bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self._fonts()
        self._search = None
        self._svar = None
        if readonly:
            self._svar = tk.StringVar(top)
            self._search = ttk.Entry(body, textvariable=self._svar, font=self._font_spec or
                                     FONT["body"])
            self._search.pack(fill="x", padx=XS, pady=(XS, XS))
            ph = tk.Label(self._search, text="Type to filter…",
                          foreground=COLOR["placeholder"], background=COLOR["card"],
                          font=self._font_spec or FONT["body"], bd=0, padx=0, pady=0,
                          anchor="w", cursor="xterm")
            ph.bind("<Button-1>", lambda e: self._focus_search())
            self._search_ph = ph

            def on_search(*_a):
                if self._dead or self._popup is None:
                    return
                try:
                    q = self._svar.get()
                    if q:
                        ph.place_forget()
                    else:
                        ph.place(x=7, rely=0.5, anchor="w")
                    self._query = q
                    self._refilter()
                except tk.TclError:
                    pass
            self._svar.trace_add("write", on_search)
            ph.place(x=7, rely=0.5, anchor="w")
            s = self._search
            for seq in ("<Down>", "<Up>", "<Prior>", "<Next>", "<Home>", "<End>"):
                s.bind(seq, self._on_search_nav)
            s.bind("<Return>", self._on_search_return)
            s.bind("<KP_Enter>", self._on_search_return)
            s.bind("<Escape>", self._on_search_escape)
            s.bind("<Tab>", self._on_search_tab)
            s.bind("<Shift-Tab>", self._on_search_tab)
            s.bind("<FocusOut>", self._on_focus_out, add="+")
        listf = tk.Frame(body, background=COLOR["card"], bd=0, highlightthickness=0)
        listf.pack(fill="both", expand=True)
        width = self._popup_width()
        need_sb = len(self._rows) > vis
        sb_w = 12 if need_sb else 0
        self._cw = width - 2 - sb_w
        self._canvas = c = tk.Canvas(listf, width=self._cw, height=vis * ROW,
                                     background=COLOR["card"], highlightthickness=0, bd=0,
                                     cursor="hand2", takefocus=0)
        self._sb = ttk.Scrollbar(listf, orient="vertical", command=self._on_scrollbar)
        c.pack(side="left", fill="both", expand=True)
        if need_sb:
            self._sb.pack(side="right", fill="y")
        self._footer = ttk.Label(body, text="", style="CardMuted.TLabel", anchor="w",
                                 font=FONT["small"], background=COLOR["card"],
                                 foreground=COLOR["muted_text"])
        self._footer.pack(fill="x", padx=S, pady=(2, XS))
        c.bind("<Configure>", self._on_canvas_configure)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Button-1>", self._on_list_click)
        top.bind("<MouseWheel>", self._on_wheel)
        top.bind("<Button-4>", self._on_wheel)
        top.bind("<Button-5>", self._on_wheel)
        top.bind("<FocusIn>", self._on_popup_focus_in)

        if query:
            if readonly:
                self._svar.set(query)
                self._search.icursor("end")
            else:
                self._query = query
                self._refilter()
        else:
            self._select_current()
            self._draw()
        self._place(width)
        top.deiconify()
        try:
            top.lift()
        except tk.TclError:
            pass
        self._bind_outside()
        self._draw_arrow()
        if readonly:
            self._focus_search()
        else:
            self.entry.focus_set()

    def _focus_search(self):
        """Focus the popup's filter field. The focus_set is asked twice: on macOS
        an override-redirect window only becomes the key window asynchronously,
        so the first ask (right after deiconify) can silently do nothing and no
        caret appears. The second ask is a no-op when the popup already closed."""
        top, search = self._popup, self._search
        if top is None or search is None:
            return

        def ask():
            try:
                if self._popup is top and not self._dead:
                    search.focus_set()
            except tk.TclError:
                pass

        ask()
        try:
            self.after(60, ask)
        except tk.TclError:
            pass

    def close(self, restore=False, focus=True):
        """Close the list. restore: put back the text there was when it opened ('normal'
        state). focus: give the focus back to the entry."""
        top = self._popup
        if top is None:
            return
        self._unbind_outside()
        if focus and not self._dead:
            try:
                self.entry.focus_set()      # before the popup goes: the app keeps the focus
            except tk.TclError:
                pass
        self._popup = None
        self._canvas = self._search = self._svar = None
        try:
            top.destroy()
        except tk.TclError:
            pass
        if restore and not self._dead:
            self.set(self._orig)
        if not self._dead:
            try:
                self._draw_arrow()
            except tk.TclError:
                pass

    def _report(self):
        try:
            self.report_callback_exception(*sys.exc_info())
        except Exception:               # noqa: BLE001
            pass

    def _fonts(self):
        spec = self._font_spec or FONT["body"]
        if getattr(self, "_font_key", None) != spec:
            self._font_key = spec
            self._f_item = tkfont.Font(self, font=spec)
            self._f_bold = tkfont.Font(self, font=spec)
            self._f_bold.configure(weight="bold")
            self._f_head = tkfont.Font(self, font=FONT["small_bold"])
            self._f_small = tkfont.Font(self, font=FONT["small"])

    def _prepare(self):
        """Display texts of the values (labels applied); lower-cased on first search."""
        self._texts = [self._label(v) for v in self._values]
        self._lows = None
        self._first_of = None

    def _lower(self):
        if self._lows is None:
            self._lows = [t.lower() for t in self._texts]
        return self._lows

    def _index_of_text(self, text):
        if self._first_of is None:
            first = {}
            for i, v in enumerate(self._values):
                t = v if isinstance(v, str) else str(v)
                if t not in first:
                    first[t] = i
            self._first_of = first
        return self._first_of.get(text)

    def _filter_idx(self, q, idxs):
        lows, texts = self._lower(), self._texts
        buckets = ([], [], [], [], [])
        for i in idxs:
            r = _match(q, texts[i], lows[i])
            if r is not None:
                buckets[r[0]].append(("i", i, r[1]))
        out = []
        for b in buckets:
            out.extend(b)
        return out

    def _build_rows(self, query):
        """[('h', title) | ('i', value index, positions)] and the number of values listed."""
        q = query.lower()
        n = len(self._values)
        rows = []
        if not q.strip():
            recents = []
            for text in recent_choices(self.recent_key):
                i = self._index_of_text(text)
                if i is not None:
                    recents.append(("i", i, ()))
            if recents:
                rows.append(("h", "Recent"))
                rows.extend(recents)
            if self._groups:
                for title, idxs in self._groups:
                    rows.append(("h", title))
                    rows.extend(("i", i, ()) for i in idxs)
            else:
                if recents:
                    rows.append(("h", "All"))
                rows.extend(("i", i, ()) for i in range(n))
            return rows, n
        if self._groups:
            count = 0
            for title, idxs in self._groups:
                found = self._filter_idx(q, idxs)
                if found:
                    rows.append(("h", title))
                    rows.extend(found)
                    count += len(found)
            return rows, count
        rows = self._filter_idx(q, range(n))
        return rows, len(rows)

    def _refilter(self):
        if self._popup is None:
            return
        self._rows, self._nmatch = self._build_rows(self._query)
        self._top = 0
        if self._query.strip():
            self._active = self._first_item(0, 1) if self._state == "readonly" else -1
        else:
            self._active = -1
            self._select_current()
        self._draw()

    def _select_current(self):
        text = self.get()
        cur = -1
        for r, row in enumerate(self._rows):
            if row[0] == "i":
                v = self._values[row[1]]
                if (v if isinstance(v, str) else str(v)) == text:
                    cur = r
                    break
        if cur < 0 and self._state == "readonly":
            cur = self._first_item(0, 1)
        self._active = cur
        self._top = 0
        if cur >= 0:
            self._ensure_visible()

    def _popup_width(self):
        w0 = max(self.winfo_width(), self.winfo_reqwidth())
        texts = self._texts
        longest = heapq.nlargest(20, range(len(texts)), key=lambda i: len(texts[i]))
        text_w = max([self._f_item.measure(texts[i]) for i in longest] or [0])
        if self._groups:
            text_w = max([text_w] + [self._f_head.measure(t) for t, _ in self._groups])
        count_w = 0
        if self._counts:
            cs = heapq.nlargest(5, (_fmt_count(n) for n in self._counts.values()), key=len)
            count_w = max([self._f_small.measure(t) for t in cs] or [0]) + S
        natural = S + 16 + text_w + count_w + S + 14 + 2
        try:
            screen = self.winfo_screenwidth()
        except tk.TclError:
            screen = 1024
        cap = max(w0, min(_MAX_POPUP, screen - 2 * S))
        return int(min(max(w0, natural), cap))

    def _place(self, width):
        top = self._popup
        top.update_idletasks()
        h = top.winfo_reqheight()
        x = self.winfo_rootx()
        below = self.winfo_rooty() + self.winfo_height()
        above = self.winfo_rooty() - h
        sh, sw = self.winfo_screenheight(), self.winfo_screenwidth()
        y = below
        if below + h > sh and above >= 0:
            y = above
        if x < sw and x + width > sw:
            x = max(0, sw - width)
        top.geometry("%dx%d+%d+%d" % (width, h, x, y))

    # ── drawing (only the rows in view) ───────────────────────────────────────
    def _draw(self):
        c = self._canvas
        if c is None:
            return
        c.delete("all")
        w = max(40, self._cw)
        rows, top, vis = self._rows, self._top, self._vis
        n = len(rows)
        cur_text = self.get()
        fi, fb, fh, fs = self._f_item, self._f_bold, self._f_head, self._f_small
        if not n:
            if self._query.strip():
                msg = "No match for “%s”" % self._query.strip()
            else:
                msg = "No items"
            c.create_text(S, ROW // 2, text=msg, anchor="w", font=fi,
                          fill=COLOR["muted_text"])
        for r in range(top, min(n, top + vis + 1)):
            row = rows[r]
            y0 = (r - top) * ROW
            ym = y0 + ROW // 2
            if row[0] == "h":
                c.create_text(S, ym + 2, text=row[1], anchor="w", font=fh,
                              fill=COLOR["muted_text"])
                continue
            idx, pos = row[1], row[2]
            v = self._values[idx]
            is_cur = (v if isinstance(v, str) else str(v)) == cur_text
            if r == self._active:
                c.create_rectangle(0, y0, w, y0 + ROW, fill=COLOR["selection"], width=0)
            x = S + 16
            right = w - S
            count = _lookup(self._counts, self._count_text, v)
            if count is not None:
                ct = _fmt_count(count)
                c.create_text(right, ym, text=ct, anchor="e", font=fs,
                              fill=COLOR["muted_text"])
                right -= fs.measure(ct) + S
            font = fb if is_cur else fi
            text = self._texts[idx]
            shown = _elide(text, font, right - x)
            if pos:
                limit = len(shown) - (1 if shown != text else 0)
                for a, b in _runs(pos):
                    if a >= limit:
                        break
                    b = min(b, limit - 1)
                    x0 = x + font.measure(shown[:a])
                    x1 = x + font.measure(shown[:b + 1])
                    c.create_rectangle(x0, y0 + 4, x1, y0 + ROW - 4,
                                       fill=COLOR["highlight"], width=0)
            if is_cur:
                c.create_text(S, ym, text=_CHECK, anchor="w", font=fb, fill=COLOR["primary"])
            c.create_text(x, ym, text=shown, anchor="w", font=font,
                          fill=COLOR["primary"] if is_cur else COLOR["text"])
        # footer and scrollbar
        total = len(self._values)
        if self._query.strip():
            foot = "%s of %s" % (_fmt_count(self._nmatch), _fmt_count(total))
        else:
            foot = "1 item" if total == 1 else "%s items" % _fmt_count(total)
        self._footer.configure(text=foot)
        if n > vis:
            self._sb.set(float(top) / n, float(min(n, top + vis)) / n)
        else:
            self._sb.set(0.0, 1.0)

    def footer_text(self):
        """The line under the list ('12 of 480', '480 items'); '' when closed."""
        return "" if self._popup is None else str(self._footer.cget("text"))

    def visible_rows(self):
        """What the open list shows now: [('header', title) | ('item', value)] (all rows,
        not only those in view); [] when closed."""
        if self._popup is None:
            return []
        return [("header", r[1]) if r[0] == "h" else ("item", self._values[r[1]])
                for r in self._rows]

    def active_value(self):
        """The highlighted value of the open list (None when none is)."""
        if self._popup is None or self._active < 0:
            return None
        return self._values[self._rows[self._active][1]]

    # ── list events ───────────────────────────────────────────────────────────
    @_guarded
    def _on_canvas_configure(self, event):
        if event.width > 1 and event.width != self._cw:
            self._cw = event.width
            self._draw()

    @_guarded
    def _on_motion(self, event):
        r = self._top + int(event.y) // ROW
        if 0 <= r < len(self._rows) and self._rows[r][0] == "i" and r != self._active:
            self._active = r
            self._draw()

    @_guarded
    def _on_list_click(self, event):
        r = self._top + int(event.y) // ROW
        if 0 <= r < len(self._rows) and self._rows[r][0] == "i":
            self._choose_row(r)
        return "break"

    @_guarded
    def _on_wheel(self, event):
        if self._popup is None:
            return None
        if getattr(event, "num", None) == 4:
            step = -3
        elif getattr(event, "num", None) == 5:
            step = 3
        else:
            d = int(event.delta or 0)
            if not d:
                return "break"
            step = -3 * (d // 120 if abs(d) >= 120 else (1 if d > 0 else -1))
        self._scroll_to(self._top + step)
        return "break"

    @_guarded
    def _on_scrollbar(self, *args):
        if self._popup is None or not args:
            return
        n = len(self._rows)
        if args[0] == "moveto":
            self._scroll_to(int(round(float(args[1]) * n)))
        elif args[0] == "scroll":
            k = int(args[1])
            self._scroll_to(self._top + (k * self._vis if args[2] == "pages" else k))

    @_guarded
    def _on_popup_focus_in(self, event):
        # 'normal' state: the typing stays in the entry even after a click in the list
        if self._state == "normal" and self._popup is not None:
            self.entry.focus_set()

    def _scroll_to(self, top):
        top = max(0, min(int(top), len(self._rows) - self._vis))
        if top != self._top:
            self._top = top
            self._draw()

    # ── keyboard in the list ──────────────────────────────────────────────────
    def _first_item(self, start, step):
        r = start
        rows = self._rows
        while 0 <= r < len(rows):
            if rows[r][0] == "i":
                return r
            r += step
        return -1

    def _ensure_visible(self):
        a = self._active
        if a < 0:
            return
        if a < self._top:
            self._top = a
            if a > 0 and self._rows[a - 1][0] == "h":
                self._top = a - 1           # the header of the first row in view too
        elif a >= self._top + self._vis:
            self._top = a - self._vis + 1

    def _move(self, target, step):
        r = self._first_item(max(0, min(target, len(self._rows) - 1)), step)
        if r < 0:
            r = self._first_item(max(0, min(target, len(self._rows) - 1)), -step)
        if r >= 0:
            self._active = r
            self._ensure_visible()
            self._draw()

    def _nav(self, keysym):
        if self._popup is None:
            return None
        a = self._active
        n = len(self._rows)
        if not n:
            return "break"
        if keysym == "Down":
            if a < 0:
                self._move(0, 1)
            elif self._first_item(a + 1, 1) >= 0:
                self._move(a + 1, 1)
        elif keysym == "Up":
            if a < 0:
                self._move(n - 1, -1)
            elif self._first_item(a - 1, -1) >= 0:
                self._move(a - 1, -1)
        elif keysym == "Next":
            self._move((0 if a < 0 else a) + self._vis, 1)
        elif keysym == "Prior":
            self._move((0 if a < 0 else a) - self._vis, -1)
        elif keysym == "Home":
            self._move(0, 1)
            self._top = 0
            self._draw()
        elif keysym == "End":
            self._move(n - 1, -1)
        return "break"

    @_guarded
    def _on_search_nav(self, event):
        return self._nav(event.keysym)

    @_guarded
    def _on_search_return(self, event):
        if self._active >= 0:
            self._choose_row(self._active)
        else:
            self.close()
        return "break"

    @_guarded
    def _on_search_escape(self, event):
        self.close(restore=True)
        return "break"

    @_guarded
    def _on_search_tab(self, event):
        back = event.keysym == "ISO_Left_Tab" or bool(event.state & 0x1)
        if self._active >= 0:
            self._choose_row(self._active)
        else:
            self.close()
        if not self._dead:
            nxt = self.entry.tk_focusPrev() if back else self.entry.tk_focusNext()
            if nxt is not None:
                nxt.focus_set()
        return "break"

    def _choose_row(self, r):
        row = self._rows[r]
        value = self._values[row[1]]
        text = value if isinstance(value, str) else str(value)
        self.close(focus=True)
        self.set(text)
        if self._state == "normal":
            try:
                self.entry.icursor("end")
                self.entry.selection_clear()
            except tk.TclError:
                pass
        record_recent(self.recent_key, text)
        if not self._dead:
            self.event_generate("<<ComboboxSelected>>")

    # ── closing on a click or focus elsewhere, or when the window moves ───────
    def _bind_outside(self):
        self._click_cmd = self.register(self._on_global_click)
        self._click_line = "%s %%W" % self._click_cmd
        try:
            script = self.tk.call("bind", "all", "<ButtonPress>")
        except tk.TclError:
            script = ""
        script = str(script or "")
        self.tk.call("bind", "all", "<ButtonPress>",
                     (script + "\n" + self._click_line) if script else self._click_line)
        tl = self.winfo_toplevel()
        self._tl = tl
        self._tl_geom = tl.winfo_geometry()
        self._tl_tag = "SearchableCombo%d" % id(self)
        tl.bindtags((self._tl_tag,) + tuple(tl.bindtags()))
        tl.bind_class(self._tl_tag, "<Configure>", self._on_toplevel_change)
        tl.bind_class(self._tl_tag, "<Unmap>", lambda e: self._on_toplevel_change(e, True))

    def _unbind_outside(self):
        line = getattr(self, "_click_line", None)
        if line:
            try:
                script = str(self.tk.call("bind", "all", "<ButtonPress>") or "")
                lines = [ln for ln in script.split("\n") if ln.strip() != line]
                self.tk.call("bind", "all", "<ButtonPress>", "\n".join(lines))
            except tk.TclError:
                pass
            try:
                self.deletecommand(self._click_cmd)
            except (tk.TclError, ValueError):
                pass
            self._click_line = None
        tag = getattr(self, "_tl_tag", None)
        if tag:
            tl = self._tl
            try:
                tl.bindtags(tuple(t for t in tl.bindtags() if t != tag))
            except tk.TclError:
                pass
            for seq in ("<Configure>", "<Unmap>"):
                try:
                    tl.unbind_class(tag, seq)
                except tk.TclError:
                    pass
            self._tl_tag = None

    def _on_global_click(self, path):
        if self._dead or self._popup is None:
            return
        try:
            path = str(path)
            mine = (str(self.entry), str(self._arrow), str(self._ph), str(self))
            if path.startswith(str(self._popup)) or path in mine:
                return
            self.close(focus=False)
        except tk.TclError:
            pass

    def _on_toplevel_change(self, event, unmapped=False):
        if self._dead or self._popup is None:
            return
        try:
            if event.widget is not self._tl and str(event.widget) != str(self._tl):
                return
            if unmapped or self._tl.winfo_geometry() != self._tl_geom:
                self.close(focus=False)
        except tk.TclError:
            pass

    @_guarded
    def _on_focus_out(self, event=None):
        if self._popup is None:
            return
        aid = self.after_idle(self._check_focus)
        self._afters.add(aid)

    def _check_focus(self):
        self._afters.clear()
        if self._dead or self._popup is None:
            return
        f = self._focus_now()
        if f is None or isinstance(f, (tk.Tk, tk.Toplevel)):
            # another program has the keyboard focus (the user switched away), or the window
            # manager handed it to a window itself: the list stays open for when the user
            # comes back; a click or key in the app closes it
            return
        p = str(f)
        if p == str(self.entry) or p.startswith(str(self._popup)):
            return
        try:
            self.close(focus=False)
        except tk.TclError:
            pass


def _runs(positions):
    """[(first, last)] of the consecutive runs in sorted positions."""
    out = []
    for p in sorted(positions):
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return [(a, b) for a, b in out]


def _elide(text, font, avail):
    """text shortened at the end with '…' to fit avail px (the start is what is matched
    most often)."""
    if avail <= 0 or font.measure(text) <= avail:
        return text
    lo, hi, best = 0, len(text), "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + "…"
        if font.measure(cand) <= avail:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best
