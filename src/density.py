"""The density chart of the Timeline: how many events fall in each stretch of time.

bin_times()    the counts per bin (and per database) of a list of times (no Tk).
nice_bins()    a readable bin width (1 minute ... 1 year) for a time span and a bin count.
DensityChart   a Canvas histogram of the events above the event grid: bars (split by
               database colour when asked), the busiest bin's count and the time axis; hover
               shows a bin's time and count; drag across the bars to select a range
               (on_range(start, end) filters the grid), click without dragging or the ×
               clears it.
"""

import bisect
from datetime import datetime, timedelta

import tkinter as tk

from tokens import COLOR as K, FONT as F

STEPS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
         timedelta(hours=1), timedelta(hours=3), timedelta(hours=6), timedelta(hours=12),
         timedelta(days=1), timedelta(days=7), timedelta(days=30), timedelta(days=91),
         timedelta(days=365)]
STEP_NAMES = ["1 minute", "5 minutes", "15 minutes", "1 hour", "3 hours", "6 hours", "12 hours",
              "1 day", "1 week", "30 days", "quarter", "1 year"]


def nice_bins(start, end, most=120):
    """(bin width, its name): the smallest step giving at most `most` bins over start..end."""
    span = max(end - start, timedelta(seconds=1))
    for step, name in zip(STEPS, STEP_NAMES):
        if span / step <= most:
            return step, name
    n = int(span / STEPS[-1] / most) + 1
    return STEPS[-1] * n, "%d years" % n


def floor_time(t, step):
    """t rounded down to a multiple of step (from 1970-01-01; days and more from midnight)."""
    epoch = datetime(1970, 1, 1)
    n = (t - epoch) // step
    return epoch + step * n


def bin_times(times, keys=None, step=None, start=None, end=None, most=120):
    """Counts of `times` (datetimes, any order) per bin.

    Returns (edges, totals, per_key, step, step_name): edges[i] is the start of bin i (the
    last edge ends the range), totals[i] its count, per_key[key][i] the count of the events of
    each key (a database) when keys (a list parallel to times) is given."""
    times = [t for t in times if t is not None]
    if not times:
        return [], [], {}, None, ""
    lo = start if start is not None else min(times)
    hi = end if end is not None else max(times)
    if hi < lo:
        lo, hi = hi, lo
    name = ""
    if step is None:
        step, name = nice_bins(lo, hi, most)
    first = floor_time(lo, step)
    n = int((hi - first) // step) + 1
    edges = [first + step * i for i in range(n + 1)]
    totals = [0] * n
    per = {}
    for i, t in enumerate(times):
        if t < first or t > hi:
            continue
        b = min(n - 1, int((t - first) // step))
        totals[b] += 1
        if keys is not None:
            k = keys[i]
            arr = per.get(k)
            if arr is None:
                arr = per[k] = [0] * n
            arr[b] += 1
    return edges, totals, per, step, name


def fmt_edge(t, step):
    if step >= timedelta(days=1):
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")


class DensityChart(tk.Canvas):
    """The histogram. set_data(times, keys, colours) draws it; on_range(start, end) is called
    after a drag selection (None, None when it is cleared)."""

    PAD_L, PAD_R, PAD_T, PAD_B = 44, 12, 10, 20

    def __init__(self, master, on_range=None, height=96, **kw):
        tk.Canvas.__init__(self, master, height=height, background=K["card"],
                           highlightthickness=1, highlightbackground=K["border"],
                           borderwidth=0, cursor="crosshair", **kw)
        self.on_range = on_range
        self.times, self.keys, self.colours = [], None, {}
        self.split = True
        self.edges, self.totals, self.per, self.step, self.step_name = [], [], {}, None, ""
        self.selection = None          # (start, end) datetimes
        self._drag = None
        self._after = None
        self.bind("<Configure>", lambda e: self._later())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self.delete("hover"))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", self._release)

    def set_data(self, times, keys=None, colours=None):
        self.times = list(times)
        self.keys = list(keys) if keys is not None else None
        self.colours = dict(colours or {})
        self.selection = None
        self._bin()
        self.redraw()

    def set_split(self, on):
        self.split = bool(on)
        self.redraw()

    def _bin(self):
        w = max(200, self.winfo_width() - self.PAD_L - self.PAD_R)
        most = max(20, min(200, w // 6))
        (self.edges, self.totals, self.per, self.step,
         self.step_name) = bin_times(self.times, self.keys, most=most)

    def _later(self):
        if self._after is None:
            try:
                self._after = self.after_idle(self._resized)
            except tk.TclError:
                self._after = None

    def _resized(self):
        self._after = None
        if self.times:
            self._bin()
        self.redraw()

    def _x_of(self, t):
        if not self.edges:
            return self.PAD_L
        lo, hi = self.edges[0], self.edges[-1]
        w = max(1, self.winfo_width() - self.PAD_L - self.PAD_R)
        span = (hi - lo).total_seconds() or 1.0
        return self.PAD_L + w * (t - lo).total_seconds() / span

    def _t_of(self, x):
        lo, hi = self.edges[0], self.edges[-1]
        w = max(1, self.winfo_width() - self.PAD_L - self.PAD_R)
        f = min(1.0, max(0.0, (x - self.PAD_L) / float(w)))
        return lo + (hi - lo) * f

    def bar_count(self):
        return len(self.find_withtag("bar"))

    def redraw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 30:
            return
        if not self.totals:
            self.create_text(W / 2, H / 2, text="Build the timeline to see when the events "
                                                 "happened", fill=K["muted_text"],
                             font=F["small"])
            return
        top, base = self.PAD_T, H - self.PAD_B
        peak = max(self.totals) or 1
        n = len(self.totals)
        order = sorted(self.per) if (self.split and self.per and len(self.per) > 1) else None
        for i in range(n):
            if not self.totals[i]:
                continue
            x0 = self._x_of(self.edges[i])
            x1 = max(x0 + 1, self._x_of(self.edges[i + 1]) - 1)
            if order is None:
                h = (base - top) * self.totals[i] / float(peak)
                self.create_rectangle(x0, base - h, x1, base, fill=K["secondary"], outline="",
                                      tags=("bar",))
                continue
            y = base
            for k in order:
                c = self.per[k][i]
                if not c:
                    continue
                h = (base - top) * c / float(peak)
                self.create_rectangle(x0, y - h, x1, y, fill=self.colours.get(k, K["secondary"]),
                                      outline="", tags=("bar",))
                y -= h
        self.create_line(self.PAD_L, base, W - self.PAD_R, base, fill=K["border"])
        self.create_text(self.PAD_L - 6, top, text=format(peak, ","), anchor="ne",
                         fill=K["muted_text"], font=F["small"])
        self.create_text(self.PAD_L - 6, base, text="0", anchor="e", fill=K["muted_text"],
                         font=F["small"])
        self.create_text(self.PAD_L, H - 2, text=fmt_edge(self.edges[0], self.step),
                         anchor="sw", fill=K["muted_text"], font=F["small"])
        self.create_text(W - self.PAD_R, H - 2, text=fmt_edge(self.edges[-1], self.step),
                         anchor="se", fill=K["muted_text"], font=F["small"])
        self.create_text(W / 2, H - 2, text="%s per bar" % self.step_name if self.step_name
                         else "", anchor="s", fill=K["muted_text"], font=F["small"])
        self._draw_selection()

    def _draw_selection(self):
        self.delete("sel")
        if self.selection is None or not self.edges:
            return
        a, b = self.selection
        x0, x1 = self._x_of(a), self._x_of(b)
        H = self.winfo_height()
        self.create_rectangle(x0, self.PAD_T - 4, x1, H - self.PAD_B, outline=K["accent"],
                              width=2, tags=("sel",))
        self.create_text(x1 - 2, self.PAD_T - 2, text="×", anchor="ne", fill=K["accent"],
                         font=F["body_bold"], tags=("sel", "sel_clear"))

    def _motion(self, e):
        self.delete("hover")
        if not self.totals or self._drag is not None:
            return
        t = self._t_of(e.x)
        i = bisect.bisect_right(self.edges, t) - 1
        if not 0 <= i < len(self.totals):
            return
        text = "%s: %s event%s" % (fmt_edge(self.edges[i], self.step),
                                   format(self.totals[i], ","),
                                   "" if self.totals[i] == 1 else "s")
        if self.per and len(self.per) > 1:
            parts = sorted(((arr[i], k) for k, arr in self.per.items() if arr[i]), reverse=True)
            text += " (" + ", ".join("%s %s" % (k, format(c, ",")) for c, k in parts[:3]) + \
                (" …" if len(parts) > 3 else "") + ")"
        x = min(max(e.x, 120), self.winfo_width() - 120)
        item = self.create_text(x, 2, text=text, anchor="n", font=F["small"],
                                fill=K["heading"], tags=("hover",))
        bb = self.bbox(item)
        if bb:
            r = self.create_rectangle(bb[0] - 4, bb[1] - 1, bb[2] + 4, bb[3] + 1,
                                      fill=K["card"], outline=K["border"], tags=("hover",))
            self.tag_raise(item, r)
        self.hover_text = text

    def _press(self, e):
        if not self.edges:
            return
        if self.find_withtag("current") and "sel_clear" in self.gettags("current"):
            self.clear_selection()
            return
        self._drag = e.x

    def _drag_move(self, e):
        if self._drag is None:
            return
        a, b = sorted((self._drag, e.x))
        self.selection = (self._t_of(a), self._t_of(b))
        self._draw_selection()

    def _release(self, e):
        if self._drag is None:
            return
        start = self._drag
        self._drag = None
        if abs(e.x - start) < 3:
            self.clear_selection()
            return
        a, b = sorted((start, e.x))
        self.select(self._t_of(a), self._t_of(b))

    def select(self, start, end):
        """Select a range (as a drag does) and report it."""
        self.selection = (start, end)
        self._draw_selection()
        if self.on_range is not None:
            self.on_range(start, end)

    def clear_selection(self):
        had = self.selection is not None
        self.selection = None
        self.delete("sel")
        if had and self.on_range is not None:
            self.on_range(None, None)
