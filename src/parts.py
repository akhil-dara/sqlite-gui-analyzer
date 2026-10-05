"""Small building blocks of the workspace UI, all drawn from the design tokens.

StatusLine   one line of status (shortened in the middle to fit; the full text is its tooltip)
             with a 'Details' link that folds out the longer text below it. Never a
             paragraph: whatever does not fit one line goes to the details.
Expander     a section header with a ▸/▾ toggle that shows or hides its body frame.
Chip         a rounded, canvas-drawn pill: a label, optional colour dot, optional count, an
             optional × to remove it; toggles (filter chips) or acts (a click callback).
ChipBar      chips in a row that wrap onto more lines rather than being cut.
Card         a white panel with a thin border, a small title and a body frame.
EmptyState   centred: a short title, one line of explanation and one action button.
HoverCard    a multi-line tooltip for the lines of a Treeview (text from a callback).
Toolbar      a row of groups separated by thin vertical rules; groups wrap as a whole.
dot_image    a small round PhotoImage in a colour (database colours in lists).
"""

import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

from tokens import COLOR as K, FONT as F, XS, S, M
from widgets import ElideLabel, FlowFrame, ToolTip, _exists, cut_list, menu_button

_DOTS = {}


def dot_image(master, colour, size=10, ring=None):
    """A round dot of `colour` (cached per interpreter); ring: an outline colour."""
    key = (str(master.tk), colour, size, ring)
    img = _DOTS.get(key)
    if img is not None:
        return img
    img = tk.PhotoImage(master=master, width=size + 2, height=size)
    r = (size - 1) / 2.0
    for y in range(size):
        row = []
        for x in range(size + 2):
            d = ((x - r) ** 2 + (y - r) ** 2) ** 0.5
            if x >= size:
                row.append(None)
            elif ring is not None and r - 1.3 < d <= r + 0.2:
                row.append(ring)
            elif d <= r - 0.2:
                row.append(colour)
            else:
                row.append(None)
        for x, c in enumerate(row):
            if c is not None:
                img.put(c, (x, y))
    _DOTS[key] = img
    return img


class StatusLine(ttk.Frame):
    """One line of status with optional details (folded out below on demand).

    set(summary, details=None): details is a list of lines (or one text). cget('text') and
    text() give the summary and the details together (what the line stands for), so code
    and tests can read everything the user can see."""

    def __init__(self, master, style="Muted.TLabel", **kw):
        ttk.Frame.__init__(self, master, **kw)
        self._style = style
        self._summary, self._details = "", []
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.label = ElideLabel(row, text="", style=style)
        self.more = ttk.Label(row, text="Details ▸", style="Link.TLabel", cursor="hand2")
        self.more.bind("<Button-1>", lambda e: self.toggle())
        self.more.bind("<Return>", lambda e: self.toggle())
        self.label.pack(side="left", fill="x", expand=True)
        self.body = tk.Text(self, height=1, wrap="word", relief="flat", borderwidth=0,
                            background=K["background"], foreground=K["muted_text"],
                            font=F["small"], padx=S, pady=2, cursor="arrow",
                            highlightthickness=0)
        self._open = False

    def set(self, summary, details=None):
        if isinstance(details, str):
            details = [d for d in details.split("\n") if d.strip()]
        self._summary = str(summary or "")
        self._details = [str(d) for d in (details or []) if str(d).strip()]
        self.label.set_text(self._summary)
        tip = self._summary + ("\n\n" + cut_list(self._details) if self._details else "")
        self.label._tip.text = tip
        if self._details:
            if not self.more.winfo_manager():
                self.more.pack(side="right", padx=(S, 0))
        else:
            if self.more.winfo_manager():
                self.more.pack_forget()
            if self._open:
                self.toggle()
        self._fill()

    def _fill(self):
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", "\n".join(self._details))
        self.body.configure(state="disabled", height=max(1, min(len(self._details), 12)))
        self.more.configure(text=("Details ▾" if self._open else "Details ▸")
                            + (" (%d)" % len(self._details) if len(self._details) > 1 else ""))

    def toggle(self):
        self._open = not self._open
        if self._open:
            self.body.pack(fill="x", pady=(2, 0))
        else:
            self.body.pack_forget()
        self._fill()

    def is_open(self):
        return self._open

    def summary(self):
        return self._summary

    def details(self):
        return list(self._details)

    def text(self):
        return "\n".join([self._summary] + self._details)

    def configure(self, cnf=None, **kw):
        """configure(text=...) sets the summary (a text with lines: the first is the summary,
        the rest the details), as a Label would take it."""
        if "text" in kw:
            lines = str(kw.pop("text") or "").split("\n")
            self.set(lines[0], lines[1:])
        if kw or cnf:
            kw.pop("wraplength", None)
            kw.pop("foreground", None)
            if kw or cnf:
                return ttk.Frame.configure(self, cnf, **kw)
        return None

    config = configure

    def cget(self, key):
        if key == "text":
            return self.text()
        return ttk.Frame.cget(self, key)


class Expander(ttk.Frame):
    """A header line with a ▸/▾ toggle and a body frame (self.body) shown when open."""

    def __init__(self, master, title, open_=False, on_toggle=None, style="Heading.TLabel",
                 **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.on_toggle = on_toggle
        self._open = False
        head = self.head = ttk.Frame(self)
        head.pack(fill="x")
        self.arrow = ttk.Label(head, text="▸", style=style, cursor="hand2", width=2)
        self.arrow.pack(side="left")
        self.title = ttk.Label(head, text=title, style=style, cursor="hand2")
        self.title.pack(side="left")
        self.extra = ttk.Label(head, text="", style="Muted.TLabel")
        self.extra.pack(side="left", padx=(S, 0))
        for w in (self.arrow, self.title):
            w.bind("<Button-1>", lambda e: self.toggle())
        self.body = ttk.Frame(self)
        if open_:
            self.toggle()

    def set_title(self, title, extra=None):
        self.title.configure(text=title)
        if extra is not None:
            self.extra.configure(text=extra)

    def toggle(self, open_=None):
        want = (not self._open) if open_ is None else bool(open_)
        if want == self._open:
            return
        self._open = want
        self.arrow.configure(text="▾" if want else "▸")
        if want:
            self.body.pack(fill="both", expand=True, pady=(XS, 0))
        else:
            self.body.pack_forget()
        if self.on_toggle is not None:
            self.on_toggle(want)

    def is_open(self):
        return self._open


def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


class Chip(tk.Canvas):
    """A rounded pill. toggle=True: clicking switches it on/off (on_change(on)); else a click
    calls on_click(). closable: an × that calls on_close(). dot: a colour dot before the
    label. count: a number after it. The chip sizes itself to its text."""

    H = 22

    def __init__(self, master, text, toggle=False, on=False, on_change=None, on_click=None,
                 closable=False, on_close=None, dot=None, count=None, tooltip=None,
                 bg=None, active=False, **kw):
        self._bg = bg or K["background"]
        # the pill's height follows the font (display scaling 100-200%)
        self.H = max(self.H, tkfont.Font(root=master, font=F["small"]).metrics("linespace") + 8)
        tk.Canvas.__init__(self, master, height=self.H, width=10, highlightthickness=0,
                           borderwidth=0, background=self._bg, cursor="hand2",
                           takefocus=1, **kw)
        self.text, self.toggle_mode, self.on = text, toggle, bool(on)
        self.on_change, self.on_click, self.on_close = on_change, on_click, on_close
        self.closable, self.dot, self.count = closable, dot, count
        self.active = active            # amber: an active filter
        self._hover = False
        self._font = tkfont.Font(font=F["small"])
        self._focus = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        # macOS fallback: ButtonRelease in case ButtonPress is swallowed
        self.bind("<ButtonRelease-1>", self._click_release)
        self.bind("<space>", lambda e: self._activate())
        self.bind("<Return>", lambda e: self._activate())
        self.bind("<Delete>", lambda e: self._close())
        self.bind("<BackSpace>", lambda e: self._close())
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._tip = ToolTip(self, tooltip) if tooltip else None
        self.draw()

    def _set_hover(self, v):
        self._hover = v
        self.draw()

    def _set_focus(self, v):
        self._focus = v
        self.draw()

    def set(self, text=None, count=None, on=None):
        if text is not None:
            self.text = text
        if count is not None:
            self.count = count
        if on is not None:
            self.on = bool(on)
        self.draw()

    def draw(self):
        self.delete("all")
        f = self._font
        label = self.text + ("  %s" % self.count if self.count is not None else "")
        tw = f.measure(label)
        x = 10
        if self.dot:
            x += 12
        w = max(44, x + tw + (18 if self.closable else 10))
        self.configure(width=w)
        h = self.H - 1
        if self.on or self.active:
            fill = K["accent_soft"] if self.active else K["primary_soft"]
            edge = K["accent"] if self.active else K["primary"]
            fg = K["text"] if self.active else K["heading"]
        else:
            fill = K["hover"] if self._hover else K["card"]
            edge = K["border"]
            fg = K["text"]
        if self._focus:
            edge = K["ring"]
        _round_rect(self, 1, 1, w - 1, h, h / 2.0, fill=fill, outline=edge)
        if self.dot:
            self.create_oval(10, h / 2.0 - 4, 18, h / 2.0 + 4, fill=self.dot, outline="")
        self.create_text(x, h / 2.0 + 0.5, text=label, anchor="w", font=F["small"], fill=fg)
        if self.closable:
            cx = w - 11
            self.create_text(cx, h / 2.0, text="×", font=F["body"], fill=K["muted_text"],
                             tags=("close",))

    def _click(self, e):
        self.focus_set()
        self._click_x = e.x
        if self.closable and e.x >= int(self.cget("width")) - 18:
            self._close()
            return "break"
        self._activate()
        return "break"

    def _click_release(self, e):
        # Fallback for platforms where ButtonPress doesn't reach the canvas:
        # if press was missed but release lands on the chip, treat as click.
        if getattr(self, "_click_x", None) is None:
            self.focus_set()
            w = int(self.cget("width"))
            if self.closable and e.x >= w - 18:
                self._close()
            else:
                self._activate()
            return "break"
        self._click_x = None

    def _activate(self):
        if self.toggle_mode:
            self.on = not self.on
            self.draw()
            if self.on_change is not None:
                self.on_change(self.on)
        elif self.on_click is not None:
            self.on_click()

    def _close(self):
        if self.closable and self.on_close is not None:
            self.on_close()


class ChipBar(FlowFrame):
    """Chips in a row that wraps; add_chip(...) takes Chip's options."""

    def __init__(self, master, bg=None, **kw):
        FlowFrame.__init__(self, master, **kw)
        self._bg = bg

    def add_chip(self, text, gap=XS, **kw):
        chip = Chip(self, text, bg=self._bg, **kw)
        self.add(chip, gap=gap)
        return chip

    def chips(self):
        return [w for w in self.items() if isinstance(w, Chip)]


class Card(ttk.Frame):
    """A white panel with a thin border: a small muted title and a body (self.body)."""

    def __init__(self, master, title="", **kw):
        ttk.Frame.__init__(self, master, style="Card.TFrame", padding=(M, S, M, S), **kw)
        self.title = ElideLabel(self, text=title, style="CardMuted.TLabel")
        if title:
            self.title.pack(anchor="w", fill="x")
        self.body = ttk.Frame(self, style="Plain.TFrame")
        self.body.pack(fill="both", expand=True)


class EmptyState(ttk.Frame):
    """Centred title, one line of explanation and an optional action button."""

    def __init__(self, master, title, text="", action=None, command=None, **kw):
        ttk.Frame.__init__(self, master, **kw)
        inner = ttk.Frame(self)
        inner.place(relx=0.5, rely=0.4, anchor="center")
        self.title = ttk.Label(inner, text=title, style="Title.TLabel")
        self.title.pack()
        self.text = ttk.Label(inner, text=text, style="Muted.TLabel", justify="center",
                              wraplength=420)
        self.text.pack(pady=(XS, S))
        self.button = ttk.Button(inner, text=action or "", style="Primary.TButton",
                                 command=command)
        if action:
            self.button.pack()

    def set(self, title=None, text=None):
        if title is not None:
            self.title.configure(text=title)
        if text is not None:
            self.text.configure(text=text)


class HoverCard(object):
    """A card-like tooltip for Treeview lines: text_of(iid) -> text or None."""

    def __init__(self, tree, text_of, delay=450):
        self.tree, self.text_of, self.delay = tree, text_of, delay
        self._tip = None
        self._after = None
        self._iid = None
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<ButtonPress>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _motion(self, e):
        iid = self.tree.identify_row(e.y)
        if iid == self._iid:
            return
        self._hide()
        self._iid = iid
        if iid:
            self._after = self.tree.after(self.delay, lambda: self._show(iid, e.x_root, e.y_root))

    def _show(self, iid, x, y):
        self._after = None
        try:
            if not self.tree.winfo_exists():
                return
            text = self.text_of(iid)
        except Exception:               # noqa: BLE001 - a hover must never fail
            return
        if not text:
            return
        tw = self._tip = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(tw, background=K["card"], highlightthickness=1,
                         highlightbackground=K["border"])
        frame.pack()
        lines = str(text).split("\n")
        tk.Label(frame, text=lines[0], background=K["card"], foreground=K["heading"],
                 font=F["body_bold"], anchor="w", justify="left").pack(
            fill="x", padx=M, pady=(S, 0))
        if len(lines) > 1:
            tk.Label(frame, text="\n".join(lines[1:]), background=K["card"],
                     foreground=K["muted_text"], font=F["small"], anchor="w", justify="left",
                     wraplength=420).pack(fill="x", padx=M, pady=(2, S))
        tw.wm_geometry("+%d+%d" % (x + 16, y + 18))

    def _hide(self, _e=None):
        if self._after is not None:
            try:
                self.tree.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
        self._iid = None


class Toolbar(FlowFrame):
    """Groups of controls separated by thin rules. group() starts a new group; add() puts a
    control in the current group (the rule and gap come from the tokens)."""

    def __init__(self, master, overflow=True, **kw):
        FlowFrame.__init__(self, master, vgap=XS, **kw)
        self._first = True
        self._new_group = False
        # a narrow window: the buttons at the end go behind 'More ▾' instead of wrapping
        self.overflow = overflow
        self.more, self.more_menu = menu_button(self, "More ▾",
                                              style="Subtle.TButton",
                                              postcommand=self._fill_more)
        self.overflowed = []

    def group(self):
        if not self._first:
            self._new_group = True

    def add(self, widget, stretch=False, gap=None, visible=True, min_width=None, before=None):
        if self._new_group and widget != "break":
            FlowFrame.add(self, _Rule(self), gap=M)
            self._new_group = False
            gap = M if gap is None else gap
        self._first = False
        return FlowFrame.add(self, widget, stretch=stretch, gap=XS if gap is None else gap,
                             visible=visible, min_width=min_width, before=before)

    @staticmethod
    def _can_overflow(w):
        return isinstance(w, (ttk.Button, ttk.Checkbutton, ttk.Menubutton, _Rule)) or \
            w.winfo_class() in ("TButton", "TCheckbutton", "TMenubutton")

    def relayout(self):
        """As FlowFrame, but when the items do not fit one line, buttons, check boxes and menu
        buttons from the end go into 'More ▾' (the rest still wraps if it must)."""
        if not self.overflow or not self.winfo_exists():
            return FlowFrame.relayout(self)
        width = self.winfo_width()
        if width <= 1:
            width = max(self.winfo_reqwidth(), 1)
        items = [it for it in self._items if it[0] != "break" and it[3] and _exists(it[0])]
        need = sum(max(it[0].winfo_reqwidth(), it[4] or 0) + it[1] for it in items)
        over = []
        if need > width:
            more_w = self.more.winfo_reqwidth() + XS
            for it in reversed(items):
                if need + more_w <= width:
                    break
                if it[2] or not self._can_overflow(it[0]):
                    continue
                over.append(it[0])
                need -= max(it[0].winfo_reqwidth(), it[4] or 0) + it[1]
            over.reverse()
        # never a lone rule left at the end of the line
        kept = [it for it in items if it[0] not in over]
        while kept and isinstance(kept[-1][0], _Rule):
            over.append(kept.pop()[0])
        self.overflowed = [w for w in over if not isinstance(w, _Rule)]
        saved = self._items
        shown = [it for it in saved if it[0] == "break" or it[0] not in over]
        if self.overflowed:
            shown = shown + [[self.more, XS, False, True, None]]
        for w in over:
            try:
                w.place_forget()
            except tk.TclError:
                pass
        if not self.overflowed:
            self.more.place_forget()
        self._items = shown
        try:
            FlowFrame.relayout(self)
        finally:
            self._items = saved
        return None

    def _fill_more(self):
        m = self.more_menu
        m.delete(0, "end")
        for w in self.overflowed:
            try:
                text = str(w.cget("text"))
                state = str(w.cget("state"))
            except tk.TclError:
                continue
            st = "disabled" if state == "disabled" else "normal"
            if w.winfo_class() == "TCheckbutton":
                var = str(w.cget("variable"))
                cmd = str(w.cget("command"))
                m.add_checkbutton(label=text, variable=var, state=st,
                                  command=(lambda c=cmd, w=w: w.tk.call(c)) if cmd else None)
            elif w.winfo_class() == "TMenubutton":
                sub = str(w.cget("menu"))
                m.add_command(label=text, state=st, command=lambda s=sub: self._post_sub(s))
            else:
                m.add_command(label=text, state=st, command=w.invoke)

    def _post_sub(self, menu_name):
        try:
            menu = self.nametowidget(menu_name)
            menu.tk_popup(self.more.winfo_rootx(),
                          self.more.winfo_rooty() + self.more.winfo_height())
            menu.grab_release()
        except (tk.TclError, KeyError):
            pass


class _Rule(tk.Frame):
    """A thin vertical rule between toolbar groups."""

    def __init__(self, master):
        tk.Frame.__init__(self, master, width=1, height=18, background=K["border"])
