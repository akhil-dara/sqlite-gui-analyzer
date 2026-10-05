"""Date and time pickers: one date-time field with a calendar popup, and a From / To range with
presets. Stdlib only (tkinter / ttk, datetime, calendar).

Text rules are those of engine.timeline.parse_when: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM',
'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS' (plus 'YYYY-MM-DDTHH:MM', fractional seconds and a
trailing 'Z' here). An end field (end=True) reads a value as the end of what it names, exactly
as parse_when(text, end=True) does: '2024-03-01' is 2024-03-01 23:59:59.999999, '... 10:15' is
10:15:59.999999. Every text a picker writes reads back through parse_when to the same value.

  parse_datetime(text, end=False)   naive datetime, None for empty text, ValueError otherwise
  format_datetime(dt, seconds=True) 'YYYY-MM-DD HH:MM:SS' ('' for None)
  month_grid(year, month, first_weekday=0)   6 x 7 dates (None outside 0001..9999)
  preset_range(name, now)           (start, end) of a preset relative to a reference time
  PRESETS                           [(name, label)] in menu order

  DateTimePicker   entry + calendar button + popup; get_utc() / set_utc(); UTC or local display
  DateRangePicker  From + To + presets menu; get_range() / set_range() / apply_preset()

Values are naive datetimes in UTC. A picker in 'local' mode shows (and reads) text in UTC plus
its offset; get_utc() always returns UTC.
"""

import calendar
import re
import time
import tkinter as tk
from datetime import date, datetime, timedelta, timezone
from tkinter import ttk

from tokens import COLOR, FONT, XS, S

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

PRESETS = [
    ("last_1h", "Last hour"),
    ("last_24h", "Last 24 hours"),
    ("last_7d", "Last 7 days"),
    ("last_30d", "Last 30 days"),
    ("this_month", "This month"),
    ("all", "All time"),
]
PRESET_LABELS = dict(PRESETS)
CUSTOM_LABEL = "Custom"
RANGE_LABEL = "Range"
PLACEHOLDER = "YYYY-MM-DD HH:MM:SS"
MAX_OFFSET = 14 * 60            # as engine.timeline.parse_offset

_FORMATS = (                    # (strptime format, precision) - parse_when's, then more
    ("%Y-%m-%d %H:%M:%S", "second"),
    ("%Y-%m-%d %H:%M", "minute"),
    ("%Y-%m-%dT%H:%M:%S", "second"),
    ("%Y-%m-%d", "day"),
    ("%Y-%m-%dT%H:%M", "minute"),
    ("%Y-%m-%d %H:%M:%S.%f", "exact"),
    ("%Y-%m-%dT%H:%M:%S.%f", "exact"),
)
_SHAPE = re.compile(r"^(\d+)-(\d+)-(\d+)(?:(?:\s+|T)(\d+):(\d+)(?::(\d+)(?:\.\d+)?)?)?$", re.I)
_HINT = "use YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS]"


# -- pure helpers --------------------------------------------------------------------------------
def _end_of(dt, precision):
    """The last microsecond of the day / minute / second dt names (no overflow at 9999)."""
    if precision == "day":
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    if precision == "minute":
        return dt.replace(second=59, microsecond=999999)
    if precision == "second":
        return dt.replace(microsecond=999999)
    return dt


def _explain(s):
    """A short message saying what is wrong with a text no format read."""
    m = _SHAPE.match(s)
    if m is None:
        return "Not a date: %s" % _HINT
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if len(m.group(1)) != 4 or not 1 <= y <= 9999:
        return "Year must have 4 digits (0001-9999)"
    if not 1 <= mo <= 12:
        return "Month must be 01-12"
    last = calendar.monthrange(y, mo)[1]
    if not 1 <= d <= last:
        return "%s %04d has %d days" % (MONTHS[mo - 1], y, last)
    if m.group(4) is not None:
        if int(m.group(4)) > 23:
            return "Hour must be 00-23"
        if int(m.group(5)) > 59:
            return "Minutes must be 00-59"
        if m.group(6) is not None and int(m.group(6)) > 59:
            return "Seconds must be 00-59"
    return "Not a date: %s" % _HINT


def parse_datetime(text, end=False):
    """Naive datetime from date text (see the module doc); None for empty text. end=True reads
    the value as the end of the day / minute / second it names, as parse_when does. Raises
    ValueError with a short message for anything else."""
    s = (text or "").strip()
    if not s:
        return None
    if s[-1:] in ("Z", "z") and s[:-1].rstrip()[-1:].isdigit():
        s = s[:-1].rstrip()
    for fmt, precision in _FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return _end_of(dt, precision) if end else dt
    raise ValueError(_explain(s))


def format_datetime(dt, seconds=True):
    """'YYYY-MM-DD HH:MM:SS' (or without seconds) for a datetime, 'YYYY-MM-DD 00:00:00' for a
    date, '' for None. Microseconds are dropped (parse_when reads none)."""
    if dt is None:
        return ""
    if not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
    text = "%04d-%02d-%02d %02d:%02d" % (dt.year, dt.month, dt.day, dt.hour, dt.minute)
    return text + (":%02d" % dt.second if seconds else "")


def _safe_date(ordinal):
    if 1 <= ordinal <= date.max.toordinal():
        return date.fromordinal(ordinal)
    return None


def month_grid(year, month, first_weekday=0):
    """6 rows x 7 dates showing a month: the weeks start on first_weekday (0 Monday .. 6
    Sunday), the leading and trailing days belong to the neighbour months. A cell before
    0001-01-01 or after 9999-12-31 is None."""
    if not 1 <= month <= 12 or not 1 <= year <= 9999:
        raise ValueError("no month %04d-%02d" % (year, month))
    first = date(year, month, 1).toordinal()
    lead = (date(year, month, 1).weekday() - first_weekday) % 7
    start = first - lead
    return [[_safe_date(start + r * 7 + c) for c in range(7)] for r in range(6)]


def preset_range(name, now):
    """(start, end) naive datetimes of a preset relative to the reference time now (the
    current time or the newest event of the data, as the caller decides): 'last_1h',
    'last_24h', 'last_7d', 'last_30d' end at now; 'this_month' runs from the first of now's
    month to now; 'all' is (None, None)."""
    if name == "all":
        return None, None
    if now is None:
        raise ValueError("preset %r needs a reference time" % name)
    if isinstance(now, datetime) and now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    spans = {"last_1h": timedelta(hours=1), "last_24h": timedelta(hours=24),
             "last_7d": timedelta(days=7), "last_30d": timedelta(days=30)}
    if name in spans:
        try:
            return now - spans[name], now
        except OverflowError:
            return datetime.min, now
    if name == "this_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now
    raise ValueError("unknown preset %r" % name)


def utc_now():
    """The current time as a naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def offset_text(minutes):
    """'UTC' or 'UTC+05:30' (engine.timeline.offset_label)."""
    if not minutes:
        return "UTC"
    sign = "-" if minutes < 0 else "+"
    return "UTC%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60)


def _shift(dt, minutes):
    if dt is None or not minutes:
        return dt
    try:
        return dt + timedelta(minutes=minutes)
    except OverflowError:
        raise ValueError("Out of range (0001-9999) in %s" % offset_text(minutes))


def _add_months(d, n):
    """d moved n months (the day clamped to the month's length); None outside 0001..9999."""
    idx = d.year * 12 + d.month - 1 + n
    y, m = divmod(idx, 12)
    if not 1 <= y <= 9999:
        return None
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _as_utc(dt):
    """A datetime / date as a naive UTC datetime (aware ones converted)."""
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _check_mode(mode, offset):
    if mode not in ("utc", "local"):
        raise ValueError("mode must be 'utc' or 'local', not %r" % (mode,))
    offset = int(offset or 0)
    if abs(offset) > MAX_OFFSET:
        raise ValueError("offset must be within +-14:00")
    return mode, offset


def _guard(fn):
    """An event handler that does nothing once its widget is gone (no traceback after a
    destroy)."""
    def run(self, *a, **k):
        if getattr(self, "_dead", False):
            return None
        try:
            return fn(self, *a, **k)
        except tk.TclError:
            if getattr(self, "_dead", False) or not _exists(self._owner()):
                return None
            raise
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _exists(widget):
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, RuntimeError):
        return False


_STYLED = set()


def _ensure_styles(widget):
    """The few picker-only styles, derived from the theme's (once per Tk interpreter and
    theme: a switch to dark made later pickers keep the light colours)."""
    import tokens
    key = (str(widget.tk), tokens.current_theme())
    if key in _STYLED:
        return
    _STYLED.add(key)
    st = ttk.Style(widget)
    on = COLOR["primary"]
    st.configure("DatePickerOn.TButton", background=on, foreground=COLOR["on_primary"],
                 font=FONT["small_bold"], padding=(6, 1), bordercolor=on, lightcolor=on,
                 darkcolor=on, relief="flat", focuscolor=COLOR["ring"])
    st.map("DatePickerOn.TButton", background=[("active", COLOR["primary_hover"])],
           lightcolor=[("active", COLOR["primary_hover"])],
           darkcolor=[("active", COLOR["primary_hover"])])
    st.configure("DatePicker.Link.TButton", background=COLOR["card"],
                 foreground=COLOR["primary"], bordercolor=COLOR["card"],
                 lightcolor=COLOR["card"], darkcolor=COLOR["card"])
    st.map("DatePicker.Link.TButton", background=[("active", COLOR["card"])])
    st.configure("DatePicker.Danger.TLabel", foreground=COLOR["danger_text"], font=FONT["small"])


# -- the calendar popup --------------------------------------------------------------------------
CELL_W, CELL_H = 30, 24


class _Popup(object):
    """The calendar of a DateTimePicker: a borderless window under (or above) the field."""

    def __init__(self, picker):
        self.picker = picker
        self._dead = False
        self._after = None
        self._had_focus = False
        self._focus_in_canvas = False
        self._hover = None
        fw = picker.first_weekday
        self.first_weekday = fw

        value = picker.get_display()
        if value is None:
            self.sel, self.has_sel = picker.today(), False
            hms = (23, 59, 59) if picker.end else (0, 0, 0)
        else:
            self.sel, self.has_sel = value.date(), True
            hms = (value.hour, value.minute, value.second)
        self.view = (self.sel.year, self.sel.month)

        win = self.win = tk.Toplevel(picker)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(background=COLOR["border"])
        box = ttk.Frame(win, style="Popover.TFrame", padding=S)
        box.pack(fill="both", expand=True)

        head = ttk.Frame(box, style="Plain.TFrame")
        head.pack(fill="x")
        self.prev_year = ttk.Button(head, text="«", width=2, style="Icon.TButton",
                                    command=lambda: self.step_view(-12), takefocus=0)
        self.prev_month = ttk.Button(head, text="‹", width=2, style="Icon.TButton",
                                     command=lambda: self.step_view(-1), takefocus=0)
        self.next_month = ttk.Button(head, text="›", width=2, style="Icon.TButton",
                                     command=lambda: self.step_view(1), takefocus=0)
        self.next_year = ttk.Button(head, text="»", width=2, style="Icon.TButton",
                                    command=lambda: self.step_view(12), takefocus=0)
        self.title = ttk.Label(head, text="", style="CardHeading.TLabel", anchor="center",
                               width=15)
        self.prev_year.pack(side="left")
        self.prev_month.pack(side="left")
        self.next_year.pack(side="right")
        self.next_month.pack(side="right")
        self.title.pack(side="left", fill="x", expand=True)
        try:
            from widgets import ToolTip
            for b, tip in ((self.prev_year, "Previous year (Shift+PageUp)"),
                           (self.prev_month, "Previous month (PageUp)"),
                           (self.next_month, "Next month (PageDown)"),
                           (self.next_year, "Next year (Shift+PageDown)")):
                ToolTip(b, tip)
        except Exception:               # noqa: BLE001 - tooltips are a nicety
            pass

        self.canvas = tk.Canvas(box, width=7 * CELL_W, height=7 * CELL_H,
                                background=COLOR["card"], highlightthickness=1,
                                highlightcolor=COLOR["ring"],
                                highlightbackground=COLOR["card"], takefocus=1,
                                cursor="hand2", borderwidth=0)
        self.canvas.pack(pady=(XS, XS))

        timerow = ttk.Frame(box, style="Plain.TFrame")
        timerow.pack(fill="x", pady=(XS, 0))
        ttk.Label(timerow, text="Time", style="CardMuted.TLabel").pack(side="left",
                                                                      padx=(0, XS))
        self.hh, self.mm, self.ss = (tk.StringVar(value="%02d" % v) for v in hms)
        self.spins = []
        for i, (var, top) in enumerate(((self.hh, 23), (self.mm, 59), (self.ss, 59))):
            if i:
                ttk.Label(timerow, text=":", style="Card.TLabel").pack(side="left")
            sp = ttk.Spinbox(timerow, from_=0, to=top, wrap=True, width=3, format="%02.0f",
                             textvariable=var, justify="center")
            sp.pack(side="left")
            self.spins.append(sp)

        zone = ttk.Frame(box, style="Plain.TFrame")
        zone.pack(fill="x", pady=(XS, 0))
        ttk.Label(zone, text="Shown in", style="CardMuted.TLabel").pack(side="left",
                                                                       padx=(0, XS))
        self.utc_btn = ttk.Button(zone, text="UTC", takefocus=0,
                                  command=lambda: self.set_zone("utc"))
        self.local_btn = ttk.Button(zone, text="Local", takefocus=0,
                                    command=lambda: self.set_zone("local"))
        self.utc_btn.pack(side="left")
        self.local_btn.pack(side="left", padx=(2, 0))

        foot = ttk.Frame(box, style="Plain.TFrame")
        foot.pack(fill="x", pady=(S, 0))
        self.today_btn = ttk.Button(foot, text="Today", style="DatePicker.Link.TButton",
                                    command=self.go_today, takefocus=0)
        self.today_btn.pack(side="left")
        self.apply_btn = ttk.Button(foot, text="Apply", style="Primary.TButton",
                                    command=self.apply)
        self.apply_btn.pack(side="right")
        self.clear_btn = ttk.Button(foot, text="Clear", style="Small.TButton",
                                    command=self.clear)
        self.clear_btn.pack(side="right", padx=(0, XS))

        c = self.canvas
        c.bind("<Button-1>", self._click)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", self._leave)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self.step_view(-1))
        c.bind("<Button-5>", lambda e: self.step_view(1))
        c.bind("<FocusIn>", self._canvas_focus)
        c.bind("<FocusOut>", self._canvas_focus)
        for key, days in (("<Left>", -1), ("<Right>", 1), ("<Up>", -7), ("<Down>", 7)):
            c.bind(key, lambda e, n=days: self._key(self.move_days, n))
        c.bind("<Home>", lambda e: self._key(self.week_edge, False))
        c.bind("<End>", lambda e: self._key(self.week_edge, True))
        c.bind("<space>", lambda e: self._key(self.select, self.sel))
        win.bind("<Prior>", lambda e: self._key(self.move_months, -1))
        win.bind("<Next>", lambda e: self._key(self.move_months, 1))
        win.bind("<Shift-Prior>", lambda e: self._key(self.move_months, -12))
        win.bind("<Shift-Next>", lambda e: self._key(self.move_months, 12))
        win.bind("<Return>", lambda e: self._key(self.apply))
        win.bind("<KP_Enter>", lambda e: self._key(self.apply))
        win.bind("<Escape>", lambda e: self._key(self.cancel))
        win.bind("<FocusIn>", self._focus_in)
        win.bind("<FocusOut>", self._focus_out)

        self._paint_zone()
        self.draw()
        self._place()

    # -- helpers
    def _owner(self):
        return self.win

    def alive(self):
        return not self._dead and _exists(self.win)

    def _key(self, fn, *a):
        if self.alive():
            try:
                fn(*a)
            except tk.TclError:
                if self.alive():
                    raise
        return "break"

    def _place(self):
        p, win = self.picker, self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        anchor = p.entry
        x, top = anchor.winfo_rootx(), anchor.winfo_rooty()
        below = top + anchor.winfo_height() + 2
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        y = below
        self.above = False
        if below + h > sh and top - h - 2 >= 0:
            y, self.above = top - h - 2, True
        x = max(0, min(x, sw - w))
        win.geometry("+%d+%d" % (x, y))
        win.deiconify()
        win.lift()
        try:
            self.canvas.focus_force()
        except tk.TclError:
            pass

    # -- drawing
    def cells(self):
        """{date: (x0, y0, x1, y1)} of the day cells drawn now."""
        return dict(self._cells)

    def draw(self):
        c = self.canvas
        c.delete("all")
        y, m = self.view
        self.title.configure(text="%s %04d" % (MONTHS[m - 1], y))
        self.grid = month_grid(y, m, self.first_weekday)
        for i in range(7):
            c.create_text(i * CELL_W + CELL_W // 2 + 1, CELL_H // 2 + 1,
                          text=WEEKDAYS[(self.first_weekday + i) % 7],
                          fill=COLOR["muted_text"], font=FONT["small_bold"])
        today = self.picker.today()
        partner = self.picker.partner_date()
        lo = hi = None
        if partner is not None and self.has_sel:
            lo, hi = min(partner, self.sel), max(partner, self.sel)
        self._cells = {}
        for r, row in enumerate(self.grid):
            for col, d in enumerate(row):
                if d is None:
                    continue
                x0, y0 = col * CELL_W + 1, (r + 1) * CELL_H + 1
                x1, y1 = x0 + CELL_W - 1, y0 + CELL_H - 1
                self._cells[d] = (x0, y0, x1, y1)
                selected = self.has_sel and d == self.sel
                fg = COLOR["text"] if d.month == m else COLOR["placeholder"]
                fill = COLOR["card"]
                if lo is not None and lo <= d <= hi:
                    fill = COLOR["primary_soft"]
                elif d == self._hover:
                    fill = COLOR["hover"]
                if selected:
                    fill, fg = COLOR["primary"], COLOR["on_primary"]
                c.create_rectangle(x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill=fill, outline="",
                                   tags=("cell",))
                if d == today:
                    c.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                       outline=COLOR["accent"], width=2, tags=("today",))
                if d == self.sel and (self._focus_in_canvas or not self.has_sel):
                    c.create_rectangle(x0, y0, x1, y1, outline=COLOR["ring"], width=1,
                                       dash=(2, 2) if not self.has_sel else None,
                                       tags=("cursor",))
                c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=str(d.day), fill=fg,
                              font=FONT["body_bold"] if selected or d == today else FONT["body"],
                              tags=("day",))

    def _paint_zone(self):
        p = self.picker
        # the computer's zone by its offset and the places using it, not a bare 'Local'
        from engine import timeline as _tl
        self.local_btn.configure(text="Local time (%s)" % _tl.offset_label(p.offset_minutes))
        on, off = "DatePickerOn.TButton", "Small.TButton"
        self.utc_btn.configure(style=on if p.mode == "utc" else off)
        self.local_btn.configure(style=on if p.mode == "local" else off)

    # -- state changes
    def date_at(self, x, y):
        for d, (x0, y0, x1, y1) in self._cells.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return d
        return None

    def select(self, d):
        if d is None:
            return
        self.sel, self.has_sel = d, True
        self.view = (d.year, d.month)
        self.draw()

    def move_days(self, n):
        d = _safe_date(self.sel.toordinal() + n)
        if d is not None:
            self.select(d)

    def move_months(self, n):
        self.select(_add_months(self.sel, n))

    def week_edge(self, end):
        back = (self.sel.weekday() - self.first_weekday) % 7
        self.move_days(6 - back if end else -back)

    def step_view(self, n):
        d = _add_months(date(self.view[0], self.view[1], 1), n)
        if d is not None and self.alive():
            self.view = (d.year, d.month)
            self.draw()

    def go_today(self):
        self.select(self.picker.today())

    def time_parts(self):
        out = []
        for var, top in ((self.hh, 23), (self.mm, 59), (self.ss, 59)):
            try:
                v = int(float(var.get().strip()))
            except (ValueError, tk.TclError):
                v = 0
            out.append(max(0, min(top, v)))
        return tuple(out)

    def chosen(self):
        """The date and time picked, in the picker's display zone."""
        h, m, s = self.time_parts()
        return datetime(self.sel.year, self.sel.month, self.sel.day, h, m, s)

    def set_zone(self, mode):
        """Show the calendar (and the field) in UTC or in local time; the picked moment stays
        the same moment."""
        p = self.picker
        if mode == p.mode or not self.alive():
            return
        try:
            moment = _shift(self.chosen(), -p.display_offset())
        except ValueError:
            moment = None
        p.set_mode(mode, p.offset_minutes, notify=True)
        if moment is not None:
            try:
                local = _shift(moment, p.display_offset())
            except ValueError:
                local = None
            if local is not None:
                self.sel = local.date()
                self.view = (self.sel.year, self.sel.month)
                self.hh.set("%02d" % local.hour)
                self.mm.set("%02d" % local.minute)
                self.ss.set("%02d" % local.second)
        self._paint_zone()
        self.draw()

    def apply(self):
        self.picker._apply_from_popup(format_datetime(self.chosen()))

    def clear(self):
        self.picker._apply_from_popup("")

    def cancel(self):
        self.picker.close(refocus=True)

    # -- events
    def _click(self, e):
        if not self.alive():
            return
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass
        self.select(self.date_at(e.x, e.y))

    def _double(self, e):
        if self.alive() and self.date_at(e.x, e.y) is not None:
            self.select(self.date_at(e.x, e.y))
            self.apply()

    def _motion(self, e):
        if not self.alive():
            return
        d = self.date_at(e.x, e.y)
        if d != self._hover:
            self._hover = d
            self.draw()

    def _leave(self, e):
        if self.alive() and self._hover is not None:
            self._hover = None
            self.draw()

    def _wheel(self, e):
        if self.alive():
            self.step_view(-1 if e.delta > 0 else 1)
        return "break"

    def _canvas_focus(self, e):
        if self.alive():
            self._focus_in_canvas = e.type == "9"      # tk.EventType.FocusIn
            self.draw()

    def _focus_in(self, e):
        self._had_focus = True

    def _focus_out(self, e):
        if not self._had_focus or not self.alive():
            return
        if self._after is None:
            try:
                self._after = self.win.after_idle(self._check_focus)
            except tk.TclError:
                self._after = None

    def _check_focus(self):
        """Close when the keyboard focus went outside the popup (a click elsewhere)."""
        self._after = None
        if not self.alive():
            return
        try:
            f = self.win.focus_get()
        except (KeyError, tk.TclError):
            return
        if f is None or isinstance(f, (tk.Tk, tk.Toplevel)):
            # another program has the keyboard focus, or the window manager handed it to a
            # window itself (no field of it): open until the user picks something else
            return
        # its own field keeps it open too (as the dropdowns do)
        if not str(f).startswith(str(self.win)) and str(f) != str(self.picker.entry):
            self.picker.close(refocus=False, by_focus=True)

    def destroy(self):
        if self._dead:
            return
        self._dead = True
        if self._after is not None:
            try:
                self.win.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# -- the date-time field -------------------------------------------------------------------------
class DateTimePicker(ttk.Frame):
    """A date-time field: an entry (typed text is checked on Enter / leaving the field: a bad
    text turns the field red with a short message under it and is not taken), a calendar
    button (or Alt+Down) opening a calendar with a time row, and the zone the text is in.

    Options: textvariable, end (a date alone means the end of that day), offset_minutes and
    mode ('utc' or 'local': the text is in UTC or in UTC + offset), on_change(dt_utc or None)
    after the user changes the value, width, first_weekday (0 Monday), placeholder,
    show_zone (the 'UTC' / 'UTC+05:30' label next to the field), on_edit() at each user edit
    of the text, on_mode(mode) when the calendar's UTC / Local toggle changes the zone.
    """

    def __init__(self, master, textvariable=None, end=False, offset_minutes=0, mode="utc",
                 on_change=None, width=20, first_weekday=0, placeholder=PLACEHOLDER,
                 show_zone=True, on_edit=None, on_mode=None, **kw):
        ttk.Frame.__init__(self, master, **kw)
        _ensure_styles(self)
        self._dead = False
        self.mode, self.offset_minutes = _check_mode(mode, offset_minutes)
        self.end = bool(end)
        self.first_weekday = int(first_weekday) % 7
        self.on_change, self.on_edit, self.on_mode = on_change, on_edit, on_mode
        self.partner = None             # the other end of a range (DateRangePicker)
        self._popup = None
        self._closed_at = 0.0
        self._quiet = 0                 # > 0 while the picker itself writes the text
        self._value = None              # committed UTC value
        self._shown = ""                # the text that value was read from
        self._error = ""
        self._marked = False            # marked wrong from outside (a range's To < From)

        self.var = textvariable if textvariable is not None else tk.StringVar(self)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width, style="TEntry")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.button = tk.Canvas(self, width=22, height=22, highlightthickness=1,
                                highlightcolor=COLOR["ring"],
                                highlightbackground=COLOR["background"],
                                background=COLOR["background"], borderwidth=0,
                                cursor="hand2", takefocus=1)
        self.button.grid(row=0, column=1, padx=(2, 0))
        self._draw_icon(False)
        self.zone_label = ttk.Label(self, text="", style="Small.TLabel")
        if show_zone:
            self.zone_label.grid(row=0, column=2, padx=(XS, 0))
        self.error_label = ttk.Label(self, text="", style="DatePicker.Danger.TLabel")
        self.error_label.grid(row=1, column=0, columnspan=3, sticky="w")
        self.error_label.grid_remove()
        self.columnconfigure(0, weight=1)

        from widgets import placeholder_label
        self.placeholder = placeholder_label(self.entry, placeholder, FONT["body"])
        self._trace = self.var.trace_add("write", self._text_written)

        e = self.entry
        e.bind("<Return>", self._enter)
        e.bind("<KP_Enter>", self._enter)
        e.bind("<FocusOut>", self._leave_entry)
        e.bind("<Alt-Down>", lambda ev: (self._toggle(), "break")[1])
        e.bind("<F4>", lambda ev: (self._toggle(), "break")[1])
        e.bind("<Escape>", self._entry_escape)
        b = self.button
        b.bind("<Button-1>", lambda ev: self._toggle())
        b.bind("<Enter>", lambda ev: self._draw_icon(True))
        b.bind("<Leave>", lambda ev: self._draw_icon(False))
        b.bind("<space>", lambda ev: (self._toggle(), "break")[1])
        b.bind("<Return>", lambda ev: (self._toggle(), "break")[1])
        try:
            from widgets import ToolTip
            ToolTip(b, "Pick a date and time (Alt+Down)")
        except Exception:               # noqa: BLE001
            pass

        self._paint_zone()
        self._update_placeholder()
        if self.var.get().strip():
            self._commit(notify=False)

    # -- helpers
    def _owner(self):
        return self

    def _draw_icon(self, hover):
        """A small calendar: a frame, a header band, two rings and a grid of dots."""
        c = self.button
        try:
            c.delete("all")
            color = COLOR["primary"] if hover else COLOR["muted_text"]
            c.create_rectangle(4, 6, 18, 18, outline=color, width=1)
            c.create_rectangle(4, 6, 18, 9, outline=color, fill=color)
            c.create_line(8, 4, 8, 8, fill=color, width=2)
            c.create_line(14, 4, 14, 8, fill=color, width=2)
            for yy in (12, 15):
                for xx in (7, 11, 15):
                    c.create_rectangle(xx - 1, yy - 1, xx + 1, yy + 1, outline="", fill=color)
        except tk.TclError:
            pass

    def _paint_zone(self):
        self.zone_label.configure(text=self.zone_text())

    def zone_text(self):
        """The zone the text is in: 'UTC' or 'UTC+05:30'."""
        return offset_text(self.display_offset())

    def display_offset(self):
        return self.offset_minutes if self.mode == "local" else 0

    def today(self):
        """Today's date in the zone the picker shows."""
        return _shift(utc_now(), self.display_offset()).date()

    def partner_date(self):
        """The date of the other end of a range, in this picker's zone (None without one)."""
        p = self.partner
        if p is None or getattr(p, "_dead", True):
            return None
        try:
            v = p.get_utc()
            return _shift(v, self.display_offset()).date() if v is not None else None
        except (ValueError, tk.TclError):
            return None

    def _set_text(self, text):
        self._quiet += 1
        try:
            if self.var.get() != text:
                self.var.set(text)
        finally:
            self._quiet -= 1
        self._update_placeholder()

    def _update_placeholder(self):
        try:
            if self.var.get():
                self.placeholder.place_forget()
            else:
                self.placeholder.place(x=4, rely=0.5, anchor="w", relwidth=1.0, width=-10)
        except tk.TclError:
            pass

    def _paint_state(self):
        bad = bool(self._error) or self._marked
        try:
            self.entry.configure(style="Invalid.TEntry" if bad else "TEntry")
            if self._error:
                self.error_label.configure(text=self._error)
                self.error_label.grid()
            else:
                self.error_label.configure(text="")
                self.error_label.grid_remove()
        except tk.TclError:
            pass

    def _read(self, text):
        """The UTC value of a text in this picker's zone (ValueError when unreadable)."""
        shown = parse_datetime(text, self.end)
        return _shift(shown, -self.display_offset()), shown

    def _commit(self, notify=True):
        """Take the text typed: normalise it, or mark it wrong. True when it is valid."""
        text = self.var.get().strip()
        before = self._value
        try:
            utc, shown = self._read(text)
        except ValueError as e:
            self._error = str(e)
            self._paint_state()
            return False
        self._error = ""
        self._value = utc
        self._shown = format_datetime(shown)
        self._set_text(self._shown)
        self._paint_state()
        if notify and utc != before and self.on_change is not None:
            self.on_change(utc)
        return True

    # -- events
    @_guard
    def _text_written(self, *_a):
        self._update_placeholder()
        if not self._quiet:
            if self.on_edit is not None:
                self.on_edit()

    @_guard
    def _enter(self, e=None):
        self._commit(notify=True)
        return "break"

    @_guard
    def _leave_entry(self, e=None):
        if self.var.get().strip() != self._shown or self._error:
            self._commit(notify=True)

    @_guard
    def _entry_escape(self, e=None):
        if self.is_open():
            self.close(refocus=True)
            return "break"
        return None

    @_guard
    def _toggle(self):
        if self.is_open():
            self.close(refocus=True)
        elif time.time() - self._closed_at > 0.25:     # not the click that just closed it
            self.open()

    def _apply_from_popup(self, text):
        if self._dead:
            return
        self._set_text(text)
        if self.on_edit is not None:
            self.on_edit()
        self._commit(notify=True)
        self.close(refocus=True)

    # -- public API
    def get_utc(self):
        """The value as a naive UTC datetime; None when empty or when the text is wrong
        (error() says why). Never raises."""
        if self._dead:
            return self._value if not self._error else None
        try:
            text = self.var.get().strip()
        except tk.TclError:
            return None
        if text != self._shown or self._error:
            if not self._commit(notify=False):
                return None
        return self._value

    def set_utc(self, dt, notify=False):
        """Show a UTC value (naive, aware or a date; None empties the field). The value taken is
        what the text shows, read back (seconds; the end of the second for an end field)."""
        dt = _as_utc(dt)
        text = format_datetime(_shift(dt, self.display_offset())) if dt is not None else ""
        self._set_text(text)
        return self._commit(notify=notify)

    def get_display(self):
        """The value in the zone the picker shows (None when empty or wrong)."""
        v = self.get_utc()
        try:
            return _shift(v, self.display_offset())
        except ValueError:
            return None

    def text(self):
        return self.var.get()

    def error(self):
        """Why the text is not taken ('' when it is fine)."""
        if not self._dead:
            self.get_utc()
        return self._error

    def validate(self):
        """Check the text now (as leaving the field does); True when it is valid."""
        return self._commit(notify=True)

    def set_mode(self, mode, offset_minutes=None, notify=False):
        """Show the text in UTC ('utc') or in UTC + offset_minutes ('local'); the value stays."""
        if offset_minutes is None:
            offset_minutes = self.offset_minutes
        mode, offset = _check_mode(mode, offset_minutes)
        value, ok = self._value, not self._error and self.var.get().strip() == self._shown
        if ok:
            value = self.get_utc()
        self.mode, self.offset_minutes = mode, offset
        self._paint_zone()
        if ok:
            try:
                self.set_utc(value)
            except ValueError as e:
                self._error = str(e)
                self._paint_state()
        if self._popup is not None and self._popup.alive():
            self._popup._paint_zone()
            self._popup.draw()
        if notify and self.on_mode is not None:
            self.on_mode(mode)

    def mark(self, wrong):
        """Colour the field as wrong (or not) for a reason outside it (a range's order)."""
        self._marked = bool(wrong)
        self._paint_state()

    def open(self):
        """Open the calendar under the field (above it when there is no room below)."""
        if self._dead or self.is_open():
            return
        if self.var.get().strip() != self._shown:
            self._commit(notify=True)
        self._popup = _Popup(self)

    def close(self, refocus=False, by_focus=False):
        p, self._popup = self._popup, None
        if p is None:
            return
        p.destroy()
        if by_focus:
            self._closed_at = time.time()
        if refocus and not self._dead:
            try:
                self.entry.focus_set()
            except tk.TclError:
                pass

    def is_open(self):
        return self._popup is not None and self._popup.alive()

    @property
    def popup(self):
        """The open calendar (_Popup) or None."""
        return self._popup if self.is_open() else None

    def destroy(self):
        self.close()
        self._dead = True
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass
        self.partner = None
        ttk.Frame.destroy(self)


# -- the range -----------------------------------------------------------------------------------
class DateRangePicker(ttk.Frame):
    """From and To date-time fields (To reads a date alone as the end of that day), a presets
    button (its text is the preset in use, 'Custom' after typing) and the zone of both.

    Options: on_change(start_utc, end_utc) after the user changes a valid range,
    reference_time() the time presets count back from (default: now, UTC), offset_minutes and
    mode (shared by both fields), width, first_weekday.
    get_range() -> (start, end), None for an open end; ValueError when a field is wrong or To
    is before From.
    """

    def __init__(self, master, on_change=None, reference_time=None, offset_minutes=0,
                 mode="utc", width=20, first_weekday=0, show_zone=True, **kw):
        ttk.Frame.__init__(self, master, **kw)
        _ensure_styles(self)
        self._dead = False
        self.on_change = on_change
        self.reference_time = reference_time or utc_now
        self._applying = 0
        self._preset = None
        self._last = None
        self._order_error = ""

        ttk.Label(self, text="From").grid(row=0, column=0, padx=(0, XS), sticky="n", pady=3)
        self.start = DateTimePicker(self, end=False, offset_minutes=offset_minutes, mode=mode,
                                    width=width, first_weekday=first_weekday, show_zone=False,
                                    on_change=self._changed, on_edit=self._edited,
                                    on_mode=self._mode_from_popup)
        self.start.grid(row=0, column=1, sticky="new")
        ttk.Label(self, text="To").grid(row=0, column=2, padx=(S, XS), sticky="n", pady=3)
        self.end = DateTimePicker(self, end=True, offset_minutes=offset_minutes, mode=mode,
                                  width=width, first_weekday=first_weekday, show_zone=False,
                                  on_change=self._changed, on_edit=self._edited,
                                  on_mode=self._mode_from_popup)
        self.end.grid(row=0, column=3, sticky="new")
        self.start.partner, self.end.partner = self.end, self.start

        self.preset_button = ttk.Button(self, text=RANGE_LABEL + " ▾",
                                        style="Small.TButton", command=self.show_presets)
        self.preset_button.grid(row=0, column=4, padx=(S, 0), sticky="n", pady=1)
        self.zone_label = ttk.Label(self, text="", style="Small.TLabel")
        self.zone_label.grid(row=0, column=5, padx=(XS, 0), sticky="n", pady=4)
        if not show_zone:
            self.zone_label.grid_remove()
        self.error_label = ttk.Label(self, text="", style="DatePicker.Danger.TLabel")
        self.error_label.grid(row=1, column=0, columnspan=6, sticky="w")
        self.error_label.grid_remove()
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        self._preset_var = tk.StringVar(self, value="")
        self.menu = tk.Menu(self, tearoff=0)
        for name, label in PRESETS:
            self.menu.add_radiobutton(label=label, value=name, variable=self._preset_var,
                                      command=lambda n=name: self._menu_pick(n))
        self.menu.add_separator()
        self.menu.add_radiobutton(label=CUSTOM_LABEL, value="custom",
                                  variable=self._preset_var, command=self._menu_custom)
        self._paint_zone()

    def _owner(self):
        return self

    # -- presets
    def show_presets(self):
        if self._dead:
            return
        try:
            b = self.preset_button
            self.menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            try:
                self.menu.grab_release()
            except tk.TclError:
                pass

    @_guard
    def _menu_pick(self, name):
        self.apply_preset(name, notify=True)

    @_guard
    def _menu_custom(self):
        self._set_preset("custom")
        self.start.entry.focus_set()

    def _set_preset(self, name):
        self._preset = name
        self._preset_var.set(name or "")
        if name in PRESET_LABELS:
            label = PRESET_LABELS[name]
        elif name == "custom":
            label = CUSTOM_LABEL
        else:
            label = RANGE_LABEL
        try:
            self.preset_button.configure(text=label + " ▾")
        except tk.TclError:
            pass

    def preset(self):
        """The preset in use ('last_24h' ...), 'custom', or None before any."""
        return self._preset

    def preset_label(self):
        return str(self.preset_button.cget("text")).replace(" ▾", "")

    def apply_preset(self, name, notify=True):
        """Fill both fields from a preset counted back from reference_time()."""
        now = self.reference_time() if name != "all" else None
        start, end = preset_range(name, now)
        self._applying += 1
        try:
            self.start.set_utc(start)
            self.end.set_utc(end)
        finally:
            self._applying -= 1
        self._set_preset(name)
        self._check(notify)

    # -- values
    def get_range(self):
        """(start_utc, end_utc), None for an open end. ValueError when a field's text is wrong
        or To is before From."""
        s, e = self.start.get_utc(), self.end.get_utc()
        if self.start.error():
            raise ValueError("From: %s" % self.start.error())
        if self.end.error():
            raise ValueError("To: %s" % self.end.error())
        if s is not None and e is not None and e < s:
            raise ValueError("‘To’ is before ‘From’")
        return s, e

    def set_range(self, start, end, notify=False):
        """Show a range (UTC; None for an open end). The presets button says 'Custom'."""
        self._applying += 1
        try:
            self.start.set_utc(start)
            self.end.set_utc(end)
        finally:
            self._applying -= 1
        self._set_preset("custom" if (start is not None or end is not None) else None)
        self._check(notify)

    def error(self):
        """What is wrong with the range ('' when nothing)."""
        try:
            self.get_range()
        except ValueError as e:
            return str(e)
        return ""

    def set_mode(self, mode, offset_minutes=None):
        """Show both fields in UTC or local time (UTC + offset_minutes)."""
        self.start.set_mode(mode, offset_minutes)
        self.end.set_mode(mode, offset_minutes)
        self._paint_zone()

    @property
    def mode(self):
        return self.start.mode

    @property
    def offset_minutes(self):
        return self.start.offset_minutes

    def _paint_zone(self):
        try:
            self.zone_label.configure(text=self.start.zone_text())
        except tk.TclError:
            pass

    @_guard
    def _mode_from_popup(self, mode):
        for p in (self.start, self.end):
            if p.mode != mode:
                p.set_mode(mode, p.offset_minutes)
        self._paint_zone()

    @_guard
    def _edited(self):
        if not self._applying:
            self._set_preset("custom")

    @_guard
    def _changed(self, _value=None):
        if not self._applying:
            self._check(True)

    def _check(self, notify):
        s = self.start.get_utc()
        e = self.end.get_utc()
        wrong = (s is not None and e is not None and e < s
                 and not self.start._error and not self.end._error)
        self._order_error = "‘To’ is before ‘From’" if wrong else ""
        self.end.mark(wrong)
        if wrong:
            self.error_label.configure(text=self._order_error)
            self.error_label.grid()
        else:
            self.error_label.configure(text="")
            self.error_label.grid_remove()
        if wrong or self.start._error or self.end._error:
            return
        value = (s, e)
        if notify and value != self._last and self.on_change is not None:
            self._last = value
            self.on_change(s, e)
        elif not notify:
            self._last = value

    def destroy(self):
        self._dead = True
        for p in (getattr(self, "start", None), getattr(self, "end", None)):
            if p is not None:
                p.close()
        ttk.Frame.destroy(self)
