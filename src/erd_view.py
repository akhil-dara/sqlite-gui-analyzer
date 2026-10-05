"""The entity-relationship diagram on a Tk canvas (engine.erd draws nothing itself).

ErdCanvas shows an ErdModel: white cards with a header strip (the table's name and row
count), one row per column (a key glyph for a primary key, an 'FK' badge for a column that
refers to another table, the declared type on the right), connectors from the exact column
rows with cardinality marks (crow's foot: many; bar: one). Declared keys are solid, links
verified by their values dashed, weaker ones dotted; links between databases amber with short
dashes; in a case each card has its database's colour on its left edge.

Mouse: drag a card to move it (its connectors follow); drag the background, the middle button
or the wheel to pan; Ctrl+wheel to zoom; hover a connector to see it end to end (both column
rows tinted); click a connector or a card for its details (callbacks); double-click a card to
open the table. Keyboard (the canvas has focus once clicked): arrows pan, + and - zoom, 0
fits, Escape clears the selection. A minimap in the corner shows every card and the part in
view; click or drag it to move there.

Many cards stay responsive: headers are drawn for every card, column rows only for the cards
in view (above LAZY_CARDS cards) and only when they are large enough to read; zoomed out
further, text is hidden instead of overlapping.
"""

import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from engine import erd
from engine.erd import EDGE_W, HEAD_H, MARK_W, PAD, ROW_H, TOGGLE_W, TYPE_GAP, end_marker
from tokens import COLOR, FAMILY, SEMIBOLD

MIN_TEXT_PX = 6             # zoomed out further, text is hidden
ROWS_MIN_SCALE = 0.55       # below this zoom a card shows only its header
ZOOM_MIN, ZOOM_MAX = 0.05, 3.0
LAZY_CARDS = 80             # above this many cards, rows are drawn only for cards in view
MINIMAP_W, MINIMAP_H = 180, 120
BOLD = "bold" if SEMIBOLD == FAMILY else "normal"
# (family, pixel size at zoom 1, weight, slant) per text kind (the kinds of engine.erd)
FONTS = {"name": (SEMIBOLD, 12, BOLD, "roman"), "col": (FAMILY, 11, "normal", "roman"),
         "pk": (SEMIBOLD, 11, BOLD, "roman"), "type": (FAMILY, 10, "normal", "roman"),
         "badge": (SEMIBOLD, 8, BOLD, "roman"), "label": (FAMILY, 10, "normal", "roman"),
         "member": (FAMILY, 11, "normal", "roman"), "rows": (FAMILY, 10, "normal", "roman"),
         "note": (FAMILY, 10, "normal", "italic")}
LINE_COLORS = {"declared": "primary", "verified": "primary", "nm": "primary",
               "weaker": "muted_text", "cross": "accent"}


class TkMetrics(object):
    """Text widths measured with the canvas fonts at zoom 1 (for engine.erd)."""

    def __init__(self, fonts):
        self.fonts = fonts
        self._w = {}

    def width(self, text, kind):
        key = (text, kind)
        w = self._w.get(key)
        if w is None:
            w = self._w[key] = self.fonts[kind].measure(text)
        return w

    def px(self, kind):
        return FONTS[kind][1]


def line_style(rel):
    """(colour, dash) of a connector."""
    return COLOR[LINE_COLORS[rel.style]], erd.STYLES[rel.style][1]


class ErdCanvas(ttk.Frame):
    """The diagram. Callbacks: on_table(key or None) when a card is clicked (None: the
    background), on_relationship(rel) when a connector is, on_open(node) on a double-click,
    on_positions(positions or None) after cards were moved, tidied (the positions to keep) or
    auto-arranged (None: forget them), on_group(gid, listed) when a group card is listed or
    folded."""

    def __init__(self, master, on_table=None, on_relationship=None, on_open=None,
                 on_positions=None, on_group=None):
        ttk.Frame.__init__(self, master)
        self.on_table = on_table
        self.on_group = on_group
        self.on_relationship = on_relationship
        self.on_open = on_open
        self.on_positions = on_positions
        self.canvas = cv = tk.Canvas(self, bg=COLOR["background"], highlightthickness=0,
                                     takefocus=1, xscrollincrement=1, yscrollincrement=1)
        cv.pack(fill="both", expand=True)
        self.minimap = tk.Canvas(self, width=MINIMAP_W, height=MINIMAP_H, bg=COLOR["card"],
                                 highlightthickness=1, highlightbackground=COLOR["border"],
                                 cursor="hand2")
        self.minimap_on = True
        self._base = self._make_fonts()
        self._fonts = self._make_fonts()
        self.metrics = TkMetrics(self._base)
        self.model = None
        self.layout = None
        self.colors = {}
        self.focus = None
        self.marks = set()
        self.scale = 1.0
        self.linked_only = False
        self._modes = {}            # card -> 'linked' | 'all' (over linked_only)
        self._full = set()          # cards listing every column
        self._expanded = set()      # groups listing their tables
        self.selected = None        # ('rel', rid) | ('card', key) | None
        self.hovered = None         # the same, under the mouse
        self._items = {}            # canvas item -> hit
        self._ctag = {}             # card key -> canvas tag of its items
        self._body = {}             # card key -> its body rectangle
        self._rows_drawn = set()
        self._rel_state = {}        # rid -> style state drawn
        self._text_hidden = False
        self._press = None
        self._drag = None
        self._auto_fit = False
        self._view_after = None
        self._elided = {}
        self._bind()

    def _make_fonts(self):
        return dict((k, tkfont.Font(self, family=f, size=-px, weight=w, slant=s))
                    for k, (f, px, w, s) in FONTS.items())

    def _bind(self):
        cv = self.canvas
        cv.configure(xscrollcommand=lambda *a: self._view_moved(),
                     yscrollcommand=lambda *a: self._view_moved())
        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_drag)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Double-Button-1>", self._on_double)
        cv.bind("<ButtonPress-2>", lambda e: (cv.scan_mark(e.x, e.y), self._user()))
        cv.bind("<B2-Motion>", lambda e: cv.scan_dragto(e.x, e.y, gain=1))
        cv.bind("<MouseWheel>", self._on_wheel)
        cv.bind("<Button-4>", lambda e: self._wheel(e, 120))
        cv.bind("<Button-5>", lambda e: self._wheel(e, -120))
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Leave>", lambda e: self.hover(None))
        cv.bind("<Configure>", self._on_configure)
        for seq, fn in (("<Left>", lambda e: self.pan(-40, 0)),
                        ("<Right>", lambda e: self.pan(40, 0)),
                        ("<Up>", lambda e: self.pan(0, -40)),
                        ("<Down>", lambda e: self.pan(0, 40)),
                        ("<plus>", lambda e: self.zoom_center(1.25)),
                        ("<equal>", lambda e: self.zoom_center(1.25)),
                        ("<KP_Add>", lambda e: self.zoom_center(1.25)),
                        ("<minus>", lambda e: self.zoom_center(1 / 1.25)),
                        ("<KP_Subtract>", lambda e: self.zoom_center(1 / 1.25)),
                        ("<Key-0>", lambda e: self.fit()),
                        ("<Escape>", lambda e: self.clear_selection())):
            cv.bind(seq, fn)
        mm = self.minimap
        mm.bind("<ButtonPress-1>", self._minimap_go)
        mm.bind("<B1-Motion>", self._minimap_go)

    # -- content -------------------------------------------------------------------------------
    def show(self, model, positions=None, colors=None, focus=None, marks=(), keep_view=False):
        """Draw a model (positions: {card key: (x, y)} kept for the cards they name)."""
        center = self.view_center() if keep_view and self.layout is not None else None
        self.model = model
        self.colors = dict(colors or {})
        self.focus = focus
        self.marks = set(marks)
        keys = set(model.cards())
        self._modes = dict((k, v) for k, v in self._modes.items() if k in keys)
        self._full &= keys
        self._expanded &= keys
        if self.selected is not None and not self._valid(self.selected):
            self.selected = None
        self.hovered = None
        self.layout = erd.arrange(model, self.metrics, self.linked_only, self._modes,
                                  self._full, self._expanded, positions)
        self.redraw()
        if center is not None:
            self._scroll_to(center[0] * self.scale, center[1] * self.scale)
        else:
            self.fit(initial=True)
            self._auto_fit = True

    def _valid(self, hit):
        if hit[0] == "rel":
            return self.model.rel(hit[1]) is not None
        return hit[1] in self.model.tables or hit[1] in self.model.groups

    def relayout(self):
        """Card sizes changed (a toggle): moved cards stay (overlaps removed), else the
        layout runs again. The view stays where it is."""
        center = self.view_center()
        pos = self.layout.positions() if self.layout.manual else None
        self.layout = erd.arrange(self.model, self.metrics, self.linked_only, self._modes,
                                  self._full, self._expanded, pos)
        self.redraw()
        self._scroll_to(center[0] * self.scale, center[1] * self.scale)
        if pos is not None and self.on_positions is not None:
            self.on_positions(self.layout.positions())

    def set_marks(self, keys):
        self.marks = set(keys)
        for key in self._body:
            self._style_card(key)

    def mode_of(self, key):
        return self._modes.get(key, "linked" if self.linked_only else "all")

    def set_linked_only(self, flag):
        """Every card lists only its linked (and key) columns, or all of them."""
        self.linked_only = bool(flag)
        self._modes = {}
        if self.model is not None:
            self.relayout()

    def toggle_card(self, key):
        """One card: linked columns only, or all."""
        if key in self.model.groups:
            self.toggle_group(key)
            return
        self._modes[key] = "all" if self.mode_of(key) == "linked" else "linked"
        self.relayout()

    def expand_card(self, key):
        """List every column of a card ('N more columns' clicked)."""
        if self.mode_of(key) == "linked":
            self._modes[key] = "all"
        else:
            self._full.add(key)
        self.relayout()

    def toggle_group(self, gid, expand=None):
        if expand is None:
            expand = gid not in self._expanded
        if expand:
            self._expanded.add(gid)
        else:
            self._expanded.discard(gid)
        self.relayout()
        if self.on_group is not None:
            self.on_group(gid, expand)

    def expanded_groups(self):
        return set(self._expanded)

    def set_expanded(self, gids):
        """The groups listing their tables (applied by the next show() or relayout())."""
        self._expanded = set(gids)

    def clear(self):
        """Nothing to draw (no database)."""
        self.model = self.layout = None
        self.selected = self.hovered = None
        self.canvas.delete("all")
        self._items, self._ctag, self._body = {}, {}, {}
        self._draw_minimap()

    def auto_arrange(self):
        """Lay the diagram out again, forgetting moved cards."""
        if self.model is None:
            return
        self.layout = erd.arrange(self.model, self.metrics, self.linked_only, self._modes,
                                  self._full, self._expanded)
        self.redraw()
        self.fit()
        if self.on_positions is not None:
            self.on_positions(None)

    def tidy(self):
        """Keep the cards where they are, without overlaps, aligned."""
        if self.layout is None:
            return
        center = self.view_center()
        self.layout.tidy()
        self.redraw()
        self._scroll_to(center[0] * self.scale, center[1] * self.scale)
        if self.on_positions is not None:
            self.on_positions(self.layout.positions())

    def get_positions(self):
        return self.layout.positions() if self.layout is not None else {}

    def set_positions(self, positions):
        if self.layout is None:
            return
        self.layout.set_positions(positions)
        if self.layout.overlaps():
            self.layout.tidy()
        self.redraw()

    def drawn_rels(self):
        """The relationships drawn (one connector each)."""
        if self.layout is None:
            return []
        return [r for r in self.model.rels if r.rid in self.layout.routes]

    def svg(self, title=""):
        return self.layout.svg(self.colors, title) if self.layout is not None else \
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>\n'

    def text_hidden(self):
        return self._text_hidden

    # -- drawing -------------------------------------------------------------------------------
    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        self._items, self._ctag, self._body = {}, {}, {}
        self._rows_drawn = set()
        self._rel_state = {}
        if self.layout is None:
            self._draw_minimap()
            return
        s = self.scale
        hide = False
        for k, f in self._fonts.items():
            px = int(math.floor(FONTS[k][1] * s))
            if k == "name" and px < MIN_TEXT_PX:
                hide = True
            f.configure(size=-max(1, px))
        self._text_hidden = hide
        for r in self.model.rels:
            if r.rid in self.layout.routes:
                self._draw_rel(r)
        for i, key in enumerate(self.model.cards()):
            self._ctag[key] = "c%d" % i
            self._draw_card(key)
        self._set_region()
        self._update_rows()
        self._apply_styles()
        self._draw_minimap()

    def _draw_rel(self, r):
        cv, s = self.canvas, self.scale
        tag = "e%d" % r.rid
        pts = self.layout.routes[r.rid]
        color, dash = line_style(r)
        flat = [v * s for p in pts for v in p]
        line = cv.create_line(*flat, fill=color, width=1.5, dash=dash, joinstyle="miter",
                              tags=("conn", "link", tag) + (("cross",) if r.cross else ()))
        self._items[line] = ("rel", r.rid)
        for at_start, card in ((True, r.src_card), (False, r.dst_card)):
            mk = end_marker(pts, at_start, card)
            if mk:
                it = cv.create_line(*[v * s for p in mk for v in p], fill=color, width=1.5,
                                    tags=("conn", "mark", tag))
                self._items[it] = ("rel", r.rid)
        lb = self.layout.labels.get(r.rid)
        if lb is not None and not self._text_hidden:
            x, y, w, h = lb
            bg = cv.create_rectangle(x * s, y * s, (x + w) * s, (y + h) * s,
                                     fill=COLOR["background"], outline="",
                                     tags=("conn", "lblbg", tag))
            t = cv.create_text((x + 3) * s, (y + h / 2.0) * s, text=r.label, anchor="w",
                               fill=COLOR["muted_text"], font=self._fonts["label"],
                               tags=("conn", "label", tag, "ztext"))
            self._items[bg] = self._items[t] = ("rel", r.rid)

    def _draw_card(self, key):
        cv, s = self.canvas, self.scale
        c = self.layout.cards[key]
        ctag = self._ctag[key]
        group = key in self.model.groups
        x, y, w, h = c.x * s, c.y * s, c.w * s, c.h * s
        tags = ("card", ctag)
        body = cv.create_rectangle(x, y, x + w, y + h, fill=COLOR["card"],
                                   outline=COLOR["border"], width=1, tags=tags + ("body",))
        head = cv.create_rectangle(x, y, x + w, y + HEAD_H * s, fill=COLOR["muted"],
                                   outline=COLOR["border"], width=1, tags=tags + ("head",))
        self._body[key] = body
        items = [body, head]
        color = self.colors.get(key[0]) if isinstance(key, tuple) else None
        if color:
            items.append(cv.create_rectangle(x, y, x + EDGE_W * s, y + h, fill=color,
                                             outline="", tags=tags + ("dbedge",)))
        state = "hidden" if self._text_hidden else "normal"
        avail = c.w - (PAD + EDGE_W) - TOGGLE_W - PAD - (
            self.metrics.width(c.sub, "rows") + 12 if c.sub else 0)
        title = self._elide(c.title, "name", avail)
        items.append(cv.create_text(x + (PAD + EDGE_W) * s, y + HEAD_H * s / 2.0, text=title,
                                    anchor="w", font=self._fonts["name"], fill=COLOR["heading"],
                                    state=state,
                                    tags=tags + ("gname" if group else "name", "ztext")))
        if c.sub:
            items.append(cv.create_text(x + w - (PAD + TOGGLE_W) * s, y + HEAD_H * s / 2.0,
                                        text=c.sub, anchor="e", font=self._fonts["rows"],
                                        fill=COLOR["muted_text"], state=state,
                                        tags=tags + ("sub", "ztext")))
        for it in items:
            self._items[it] = ("card", key)
        if not group:
            glyph = "▸" if self.mode_of(key) == "linked" else "▾"
            t = cv.create_text(x + w - PAD * s, y + HEAD_H * s / 2.0, text=glyph, anchor="e",
                               font=self._fonts["col"], fill=COLOR["muted_text"], state=state,
                               tags=tags + ("toggle", "ztext"))
            self._items[t] = ("toggle", key)
        self._style_card(key)

    def _style_card(self, key):
        body = self._body.get(key)
        if body is None:
            return
        cv = self.canvas
        if self.selected == ("card", key):
            cv.itemconfigure(body, outline=COLOR["primary"], width=2)
        elif key in self.marks:
            cv.itemconfigure(body, outline=COLOR["accent"], width=2)
        else:
            cv.itemconfigure(body, outline=COLOR["border"], width=1)
        heads = [i for i in cv.find_withtag(self._ctag[key]) if "head" in cv.gettags(i)]
        for i in heads:
            cv.itemconfigure(i, fill=COLOR["primary_soft"] if key == self.focus
                             else COLOR["muted"])

    def _elide(self, text, kind, avail):
        """The text, cut with '…' when wider than avail pixels at zoom 1 (the full text is in
        the side panel and the tooltips)."""
        key = (text, kind, int(avail))
        hit = self._elided.get(key)
        if hit is not None:
            return hit
        m = self.metrics
        out = text
        if m.width(text, kind) > avail:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if m.width(text[:mid] + "…", kind) <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            out = text[:lo] + "…"
        self._elided[key] = out
        return out

    def _draw_rows(self, key):
        cv, s = self.canvas, self.scale
        c = self.layout.cards[key]
        ctag = self._ctag[key]
        group = key in self.model.groups
        tags = ("card", ctag, "row")
        x = c.x * s
        for i, row in enumerate(c.rows):
            y0 = (c.y + HEAD_H + ROW_H * i) * s
            cy = y0 + ROW_H * s / 2.0
            items = []
            hit = ("row", key, i)
            if row.kind == "col":
                col = row.col
                if col.pk:
                    kx = x + PAD * s
                    k = 3 * s
                    items.append(cv.create_oval(kx, cy - k, kx + 2 * k, cy + k,
                                                outline=COLOR["warning"], width=1.5,
                                                tags=tags + ("pkmark",)))
                    items.append(cv.create_line(kx + 2 * k, cy, kx + 4.3 * k, cy, kx + 4.3 * k,
                                                cy + k, kx + 4.3 * k, cy, kx + 3.4 * k, cy,
                                                kx + 3.4 * k, cy + k, fill=COLOR["warning"],
                                                width=1.5, tags=tags + ("pkmark",)))
                if col.fk:
                    bx = x + (PAD + 16) * s
                    items.append(cv.create_rectangle(bx, cy - 6 * s, bx + 17 * s, cy + 6 * s,
                                                     fill=COLOR["primary_soft"], outline="",
                                                     tags=tags + ("fkmark",)))
                    items.append(cv.create_text(bx + 8.5 * s, cy, text="FK",
                                                font=self._fonts["badge"],
                                                fill=COLOR["primary"],
                                                tags=tags + ("fkmark", "ztext")))
                type_w = self.metrics.width(row.type, "type") if row.type else 0
                kind = "pk" if col.pk else "col"
                name = self._elide(row.text, kind, c.w - MARK_W - PAD - type_w -
                                   (TYPE_GAP if type_w else 0))
                items.append(cv.create_text(x + MARK_W * s, cy, text=name, anchor="w",
                                            font=self._fonts[kind], fill=COLOR["text"],
                                            tags=tags + ("colname", "ztext")))
                if row.type:
                    items.append(cv.create_text((c.x + c.w - PAD) * s, cy, text=row.type,
                                                anchor="e", font=self._fonts["type"],
                                                fill=COLOR["muted_text"],
                                                tags=tags + ("coltype", "ztext")))
            elif row.kind == "member":
                hit = ("member", key, row.node)
                if row.node in self.marks:
                    items.append(cv.create_rectangle((c.x + 1) * s, y0, (c.x + c.w - 1) * s,
                                                     y0 + ROW_H * s, fill=COLOR["highlight"],
                                                     outline="", tags=tags + ("match",)))
                name = self._elide(row.text, "member", c.w - 2 * PAD - TYPE_GAP -
                                   self.metrics.width(row.type, "rows"))
                items.append(cv.create_text(x + PAD * s, cy, text=name, anchor="w",
                                            font=self._fonts["member"], fill=COLOR["text"],
                                            tags=tags + ("member", "ztext")))
                items.append(cv.create_text((c.x + c.w - PAD) * s, cy, text=row.type,
                                            anchor="e", font=self._fonts["rows"],
                                            fill=COLOR["muted_text"],
                                            tags=tags + ("coltype", "ztext")))
            else:
                if row.kind == "more":
                    hit = ("more", key)
                elif row.kind == "toggle":
                    hit = ("toggle", key)
                left = PAD if group else MARK_W
                font = "col" if row.kind == "pattern" else "note"
                items.append(cv.create_text(x + left * s, cy,
                                            text=self._elide(row.text, font,
                                                             c.w - left - PAD),
                                            anchor="w", font=self._fonts[font],
                                            fill=COLOR["text"] if row.kind == "pattern"
                                            else COLOR["primary"] if row.kind in
                                            ("more", "toggle") else COLOR["muted_text"],
                                            tags=tags + (row.kind, "ztext")))
            for it in items:
                self._items[it] = hit
        self._rows_drawn.add(key)

    def rows_shown(self):
        """False when zoomed out so far that cards show only their headers."""
        return self.scale >= ROWS_MIN_SCALE and not self._text_hidden

    def _update_rows(self):
        """Draw the column rows of the cards in view (every card when there are few)."""
        if self.layout is None:
            return
        if not self.rows_shown():
            if self._rows_drawn:
                self.canvas.delete("row")
                self._rows_drawn = set()
            return
        keys = self.model.cards()
        if len(keys) > LAZY_CARDS:
            x0, y0, x1, y1 = self.view_rect()
            mx, my = (x1 - x0) * 0.5, (y1 - y0) * 0.5
            keys = [k for k in keys if k not in self._rows_drawn and erd._overlap(
                self.layout.cards[k].rect(), (x0 - mx, y0 - my, x1 - x0 + 2 * mx,
                                              y1 - y0 + 2 * my))]
        for k in keys:
            if k not in self._rows_drawn:
                self._draw_rows(k)

    # -- styles: hover and selection -----------------------------------------------------------
    def _emphasis(self):
        """{rid: 'hover' | 'selected'} and the rel whose rows are tinted."""
        out = {}
        tint = None
        for hit, state in ((self.selected, "selected"), (self.hovered, "hover")):
            if hit is None:
                continue
            if hit[0] == "rel":
                out[hit[1]] = state
                tint = hit[1]
            else:
                # the hovered part is styled after (over) the selected one
                for r in self.model.rels_of(hit[1]):
                    out[r.rid] = state
        return out, tint

    def _apply_styles(self):
        if self.layout is None:
            return
        cv = self.canvas
        emph, tint = self._emphasis()
        for r in self.model.rels:
            want = emph.get(r.rid)
            if self._rel_state.get(r.rid, "none") == (want or "none"):
                continue
            self._rel_state[r.rid] = want or "none"
            color, _dash = line_style(r)
            width = 1.5
            if want == "hover":
                color, width = COLOR["secondary"], 3
            elif want == "selected":
                color, width = COLOR["primary"], 2.5
            for it in cv.find_withtag("e%d" % r.rid):
                typ = cv.type(it)
                if typ == "line":
                    cv.itemconfigure(it, fill=color, width=width if "link" in cv.gettags(it)
                                     else max(1.5, width - 1))
            if want:
                cv.tag_raise("e%d" % r.rid, "conn")
        cv.delete("hl")
        if tint is not None:
            r = self.model.rel(tint)
            for key, port in ((r.src, r.src_col), (r.dst, r.dst_col)):
                self._tint_row(key, port)

    def _tint_row(self, key, port):
        c = self.layout.cards.get(key)
        if c is None:
            return
        i = c.ports.get(port)
        if i is None and port is not None:
            i = c.ports.get(port.lower())
        if i is None:
            return
        cv, s = self.canvas, self.scale
        y0 = (c.y + HEAD_H + ROW_H * i) * s
        r = cv.create_rectangle((c.x + 1) * s, y0, (c.x + c.w - 1) * s, y0 + ROW_H * s,
                                fill=COLOR["primary_soft"], outline="",
                                tags=("hl", "card", self._ctag[key]))
        cv.tag_raise(r, self._body[key])
        dbedge = [i for i in cv.find_withtag(self._ctag[key]) if "dbedge" in cv.gettags(i)]
        for it in dbedge:
            cv.tag_raise(it, r)
        self._items[r] = ("row", key, i)

    def hover(self, hit):
        """Hover a card or a connector (None: nothing): its connectors stand out; a hovered
        connector's two column rows are tinted."""
        if hit == self.hovered:
            return
        self.hovered = hit
        self._apply_styles()
        self.canvas.delete("tip")

    def select_rel(self, rid):
        self.selected = ("rel", rid) if rid is not None else None
        self._restyle_all()

    def select_card(self, key):
        self.selected = ("card", key) if key is not None else None
        self._restyle_all()

    def clear_selection(self):
        had = self.selected is not None
        self.selected = None
        self._restyle_all()
        if had and self.on_table is not None:
            self.on_table(None)

    def _restyle_all(self):
        for key in self._body:
            self._style_card(key)
        self._apply_styles()

    # -- geometry the tab and the tests ask ----------------------------------------------------
    def card_rect(self, key):
        """(x0, y0, x1, y1) of a card on the canvas."""
        return tuple(self.canvas.coords(self._body[key]))

    def row_y(self, key, port):
        """The canvas y of a column row's centre."""
        c = self.layout.cards[key]
        return (c.y + c.port_off(port)) * self.scale

    def rel_line(self, rid):
        for it in self.canvas.find_withtag("e%d" % rid):
            if "link" in self.canvas.gettags(it):
                return it
        return None

    def view_rect(self):
        """The part of the diagram in view, in diagram coordinates (x0, y0, x1, y1)."""
        cv = self.canvas
        vw, vh = self._view_size()
        s = self.scale
        x0, y0 = cv.canvasx(0), cv.canvasy(0)
        return (x0 / s, y0 / s, (x0 + vw) / s, (y0 + vh) / s)

    def view_center(self):
        x0, y0, x1, y1 = self.view_rect()
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    # -- zoom, scroll, fit ---------------------------------------------------------------------
    def _view_size(self):
        cv = self.canvas
        vw, vh = cv.winfo_width(), cv.winfo_height()
        if vw <= 1 or vh <= 1:
            vw, vh = max(cv.winfo_reqwidth(), 400), max(cv.winfo_reqheight(), 300)
        return vw, vh

    def _set_region(self):
        x0, y0, x1, y1 = self.layout.bounds
        s = self.scale
        vw, vh = self._view_size()
        self.canvas.configure(scrollregion=(x0 * s - vw / 2.0, y0 * s - vh / 2.0,
                                            x1 * s + vw / 2.0, y1 * s + vh / 2.0))

    def _scroll_to(self, cx, cy, vx=None, vy=None):
        """Scroll so the canvas point (cx, cy) shows at view position (vx, vy) (the middle by
        default)."""
        cv = self.canvas
        vw, vh = self._view_size()
        vx = vw / 2.0 if vx is None else vx
        vy = vh / 2.0 if vy is None else vy
        region = [float(v) for v in str(cv.cget("scrollregion")).split()]
        if len(region) != 4:
            return
        rw, rh = region[2] - region[0], region[3] - region[1]
        cv.xview_moveto(max(0.0, (cx - vx - region[0]) / rw))
        cv.yview_moveto(max(0.0, (cy - vy - region[1]) / rh))
        self._view_changed()

    def set_scale(self, scale, vx=None, vy=None):
        """Zoom to a scale keeping the diagram point at view position (vx, vy) in place."""
        if self.layout is None:
            return
        scale = max(ZOOM_MIN, min(ZOOM_MAX, scale))
        vw, vh = self._view_size()
        vx = vw / 2.0 if vx is None else vx
        vy = vh / 2.0 if vy is None else vy
        cv = self.canvas
        wx, wy = cv.canvasx(vx) / self.scale, cv.canvasy(vy) / self.scale
        self.scale = scale
        self.redraw()
        self._scroll_to(wx * scale, wy * scale, vx, vy)

    def zoom(self, factor, x=None, y=None):
        self._user()
        self.set_scale(self.scale * factor, x, y)

    def zoom_center(self, factor):
        self.zoom(factor)

    def pan(self, dx, dy):
        self._user()
        self.canvas.xview_scroll(int(dx), "units")
        self.canvas.yview_scroll(int(dy), "units")
        self._view_changed()

    def fit(self, initial=False):
        """Zoom so the whole diagram shows. On opening (initial) it is not enlarged, and not
        shrunk below the size where text can be read: then it is centred on the focused
        card (or the diagram's middle)."""
        if self.layout is None:
            return
        x0, y0, x1, y1 = self.layout.bounds
        vw, vh = self._view_size()
        want = min(vw / max(1.0, x1 - x0), vh / max(1.0, y1 - y0))
        if initial:
            want = min(1.0, max(want, ROWS_MIN_SCALE + 0.05))
        want = max(ZOOM_MIN, min(ZOOM_MAX, want))
        if abs(want - self.scale) > 1e-6 or not self._body:
            self.scale = want
            self.redraw()
        s = self.scale
        centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        if initial and self.focus in self.layout.cards and \
                ((x1 - x0) * s > vw or (y1 - y0) * s > vh):
            c = self.layout.cards[self.focus]
            centre = (c.x + c.w / 2.0, c.y + c.h / 2.0)
        self._scroll_to(centre[0] * s, centre[1] * s)

    def see(self, key):
        """Scroll a card into the middle of the view (zoomed in to read it if needed)."""
        if self.layout is None or key not in self.layout.cards:
            return
        if not self.rows_shown():
            self.scale = 1.0
            self.redraw()
        c = self.layout.cards[key]
        s = self.scale
        self._scroll_to((c.x + c.w / 2.0) * s, (c.y + c.h / 2.0) * s)

    def _user(self):
        self._auto_fit = False

    def _on_configure(self, event):
        if self.layout is None:
            return
        self._set_region()
        if self._auto_fit:
            self.fit(initial=True)
        self._view_changed()

    def _view_moved(self):
        if self._view_after is None:
            try:
                self._view_after = self.after_idle(self._view_changed)
            except tk.TclError:
                pass

    def _view_changed(self):
        self._view_after = None
        try:
            self._update_rows()
            self._update_viewport()
        except tk.TclError:
            pass

    # -- minimap -------------------------------------------------------------------------------
    def show_minimap(self, on):
        self.minimap_on = bool(on)
        self._draw_minimap()

    def _draw_minimap(self):
        mm = self.minimap
        mm.delete("all")
        if not self.minimap_on or self.layout is None or not self.layout.cards:
            mm.place_forget()
            return
        if not mm.winfo_manager():
            mm.place(relx=1.0, rely=1.0, x=-8, y=-8, anchor="se")
        x0, y0, x1, y1 = self.layout.bounds
        f = min((MINIMAP_W - 8) / max(1.0, x1 - x0), (MINIMAP_H - 8) / max(1.0, y1 - y0))
        ox = (MINIMAP_W - (x1 - x0) * f) / 2.0
        oy = (MINIMAP_H - (y1 - y0) * f) / 2.0
        self._mm = (f, ox, oy, x0, y0)
        for key, c in sorted(self.layout.cards.items(), key=lambda kv: repr(kv[0])):
            fill = COLOR["primary_soft"] if key == self.focus else COLOR["muted"]
            mm.create_rectangle(ox + (c.x - x0) * f, oy + (c.y - y0) * f,
                                ox + (c.x + c.w - x0) * f, oy + (c.y + c.h - y0) * f,
                                fill=fill, outline=COLOR["border"], tags=("mcard",))
        mm.create_rectangle(0, 0, 0, 0, outline=COLOR["primary"], width=1.5,
                            tags=("viewport",))
        self._update_viewport()

    def _update_viewport(self):
        mm = getattr(self, "_mm", None)
        if mm is None or not self.minimap.find_withtag("viewport"):
            return
        f, ox, oy, bx, by = mm
        x0, y0, x1, y1 = self.view_rect()
        self.minimap.coords("viewport", ox + (x0 - bx) * f, oy + (y0 - by) * f,
                            ox + (x1 - bx) * f, oy + (y1 - by) * f)

    def minimap_viewport(self):
        return tuple(self.minimap.coords("viewport"))

    def _minimap_go(self, event):
        mm = getattr(self, "_mm", None)
        if mm is None:
            return
        self._user()
        f, ox, oy, bx, by = mm
        wx, wy = (event.x - ox) / f + bx, (event.y - oy) / f + by
        self._scroll_to(wx * self.scale, wy * self.scale)

    # -- mouse ---------------------------------------------------------------------------------
    def _hit(self, event):
        cv = self.canvas
        x, y = cv.canvasx(event.x), cv.canvasy(event.y)
        for item in reversed(cv.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            hit = self._items.get(item)
            if hit is not None:
                return hit
        return None

    def _card_of(self, hit):
        if hit is None or hit[0] == "rel":
            return None
        return hit[1]

    def _on_press(self, event):
        cv = self.canvas
        cv.focus_set()
        hit = self._hit(event)
        self._press = (event.x, event.y, hit)
        self._moved = False
        key = self._card_of(hit)
        if key is not None:
            c = self.layout.cards[key]
            self._drag = (key, event.x, event.y, c.x, c.y)
        else:
            self._drag = None
            cv.scan_mark(event.x, event.y)

    def _on_drag(self, event):
        if self._press is None:
            return
        px, py, _hit = self._press
        if not self._moved and abs(event.x - px) <= 4 and abs(event.y - py) <= 4:
            return
        self._moved = True
        self._user()
        if self._drag is None:
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            return
        key, sx, sy, cx, cy = self._drag
        s = self.scale
        self.move_card(key, cx + (event.x - sx) / s, cy + (event.y - sy) / s, notify=False)

    def move_card(self, key, x, y, notify=True):
        """Move a card to (x, y) (diagram coordinates); its connectors follow."""
        c = self.layout.cards[key]
        s = self.scale
        dx, dy = (x - c.x) * s, (y - c.y) * s
        rids = self.layout.move(key, x, y)
        self.canvas.move(self._ctag[key], dx, dy)
        self._redraw_rels(rids)
        if notify:
            self._moved_done()

    def _redraw_rels(self, rids):
        cv = self.canvas
        for rid in rids:
            tag = "e%d" % rid
            for it in cv.find_withtag(tag):
                self._items.pop(it, None)
            cv.delete(tag)
            self._rel_state.pop(rid, None)
            self._draw_rel(self.model.rels[rid])
            if cv.find_withtag("card"):
                cv.tag_lower(tag, "card")
        self._apply_styles()

    def _moved_done(self):
        self.layout._bounds()
        self._set_region()
        self._draw_minimap()
        if self.on_positions is not None:
            self.on_positions(self.layout.positions())

    def _on_release(self, event):
        press, self._press = self._press, None
        drag, self._drag = self._drag, None
        if press is None:
            return
        if self._moved:
            if drag is not None:
                self._moved_done()
            return
        self.click(press[2])

    def click(self, hit):
        """What a click on a part of the diagram does."""
        if hit is None:
            self.clear_selection()
            return
        kind = hit[0]
        if kind == "rel":
            self.select_rel(hit[1])
            if self.on_relationship is not None:
                self.on_relationship(self.model.rel(hit[1]))
        elif kind == "toggle":
            self.toggle_card(hit[1])
        elif kind == "more":
            self.expand_card(hit[1])
        elif kind == "member":
            self.select_card(hit[1])
            if self.on_table is not None:
                self.on_table(hit[2])
        else:
            key = hit[1]
            if key in self.model.groups and kind == "card":
                self.toggle_group(key)
            self.select_card(key)
            if self.on_table is not None:
                self.on_table(key)

    def _on_double(self, event):
        hit = self._hit(event)
        if hit is None or self.on_open is None:
            return
        if hit[0] == "member":
            self.on_open(hit[2])
        elif hit[0] in ("card", "row") and isinstance(hit[1], tuple):
            self.on_open(hit[1])

    def _on_motion(self, event):
        hit = self._hit(event)
        self.hover(hit)
        cv = self.canvas
        cv.delete("tip")
        if hit is not None and hit[0] == "rel":
            r = self.model.rel(hit[1])
            x, y = cv.canvasx(event.x) + 14, cv.canvasy(event.y) + 14
            t = cv.create_text(x + 6, y + 4, anchor="nw", fill=COLOR["tip_text"],
                               font=self._base["label"], tags=("tip",),
                               text="%s\n%s\n%s" % (r.title(), r.kind_text(), r.card_text()))
            bb = cv.bbox(t)
            bg = cv.create_rectangle(bb[0] - 6, bb[1] - 4, bb[2] + 6, bb[3] + 4,
                                     fill=COLOR["tip"], outline="", tags=("tip",))
            cv.tag_lower(bg, t)

    def _on_wheel(self, event):
        self._wheel(event, event.delta)

    def _wheel(self, event, delta):
        if event.state & 0x4:
            self.zoom(1.15 if delta > 0 else 1 / 1.15, event.x, event.y)
            return
        self._user()
        step = -60 if delta > 0 else 60
        if event.state & 0x1:
            self.canvas.xview_scroll(step, "units")
        else:
            self.canvas.yview_scroll(step, "units")
        self._view_changed()
