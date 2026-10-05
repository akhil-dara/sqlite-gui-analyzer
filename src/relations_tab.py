"""The Relationships tab: the tables that have links, one table's links, a diagram, and the
list of every link.

The links come from engine.relations (the App maps them in the background after a database
opens, see relations_view.RelationWindows). Confident links - declared foreign keys, and name
links whose values were found - are shown; weaker ones in their own collapsed section, and in
All links when asked for.

Three views share one search field (SearchBox: live, Enter for the next match) and one
selected table:
- Tables (the default): the tables that have links, searchable, and the selected table's card:
  'Refers to' and 'Referred by' (tables linked alike grouped: '142 tables via message_row_id →
  _id'), each line with its strength, the evidence and row counts, and a collapsed 'Weaker
  links (N)'.
- Diagram: an entity-relationship diagram (engine.erd, drawn by erd_view.ErdCanvas) of the
  selected table and the tables linked to it (1 or 2 links away); Overview shows the whole
  database. Each table is a card listing its columns (key and FK marks, types), each link a
  connector between the exact columns with its cardinality; tables linked alike are one group
  card, junction tables can be drawn as many-to-many (N:M), cards can list only their linked
  columns. Cards can be dragged (their positions are kept in the case state per view), tidied
  or arranged again. Click a connector for its cardinality, evidence and a sample JOIN, a card
  for its links.
- All links: every link in a list, as exported to CSV.
Tables without rows are hidden unless 'Show empty tables (N)' is ticked: their links cannot be
checked against values.

Tables are identified as (database, table): DB ('main') when one database is open; in a case
of several, the database's name, and the links between databases (engine.crossdb, always
'matched by value') are drawn in their own style (orange, short dashes).
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from constants import C
from engine import erd, limits
from engine.csvcells import csv_writer
from engine.linkgraph import (CROSS_STYLE, DB, LINE_STYLES, CROSS_COLOR,  # noqa: F401
                              Link, LinkGraph, ValueLink, col_name, node_label)
from erd_view import LINE_COLORS, ErdCanvas
from tokens import COLOR, FAMILY, FONT
from utils import write_allowed
from widgets import FlowFrame, SearchBox, ToolTip, TreeviewTooltip, _in_view, menu_button, wrap_to_width

ALL = "(all linked tables)"
POSITIONS = "relationships_diagram"     # the case state section of the moved cards


class TabGraph(LinkGraph):
    """The links in scope (engine.linkgraph: the tables, their neighbours, Find, the most
    linked table); shown() is what the diagram draws: one connector per relationship."""

    def __init__(self, links, view=None):
        LinkGraph.__init__(self, links)
        self.view = view

    def shown(self):
        return self.view.drawn_rels() if self.view is not None else []


def _strength_sort(l):
    return {"Declared": 0, "Strong": 1, "Likely": 2, "Weak": 3}[l.strength()]


class RelationsTab(ttk.Frame):
    """The Relationships tab (app.py adds it to the notebook)."""

    LIST_COLUMNS = (("from", "From", 230), ("to", "To", 230), ("score", "Strength", 70),
                    ("kind", "Link", 140), ("reason", "Why", 360), ("overlap", "Values found", 90),
                    ("rows", "Rows (from / to)", 140), ("dbs", "Database", 180))

    LEGEND = ("Drag a card to move it, the background (or the wheel) to pan; Ctrl+wheel: "
              "zoom; hover a line to see both columns; click a line for its cardinality and "
              "a sample JOIN, a card for its links, a group to list its tables; double-click "
              "a card to browse the table.")

    def __init__(self, master, app):
        ttk.Frame.__init__(self, master)
        self.app = app
        self.manager = app.relations
        self.all_links = []
        self.shown_links = []
        self.graph = TabGraph([])
        self.model = None           # the diagram's engine.erd.ErdModel
        self._draw_pending = False  # draw() waits for the tab to be shown
        self.bind("<Map>", lambda e: self.after_idle(self._draw_if_pending), add="+")
        self.focus = None           # the selected table (db, table)
        self.overview = False       # the Diagram shows the whole database
        self._multi = False
        self._members = {}          # database key of a node -> case member
        self._expanded = set()      # group ids listed table by table (by the user)
        self._search_groups = set()  # groups listing their tables for Find's matches
        self._matches = []          # the diagram's tables matching Find
        self._selected = None       # ('node', key) | ('group', gid) | ('rel', rel)
        self._match_i = -1
        self._spec_cache = {}       # node -> engine.erd.TableSpec (None: unknown)
        self._positions = {}        # view -> {card: (x, y)} moved cards (no case state)
        self._rows = {}             # node -> row count (None: not counted)
        self._table_iids = {}       # table list iid -> node
        self._card_rows = {}        # card tree iid -> Link
        self._build()
        self.manager.listeners.append(self.on_state)

    def columns(self):
        """All links' columns: the Database column only in a case of several databases."""
        return self.LIST_COLUMNS if self._multi else self.LIST_COLUMNS[:-1]

    # -- layout --------------------------------------------------------------------------------
    def _build(self):
        top = self.top_bar = FlowFrame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        if hasattr(self.app, "scopes"):
            from scope import ScopePicker
            # in a case: the databases whose links are shown
            self.scope_picker = top.add(ScopePicker(top, self.app, "relations",
                                                    on_change=self.reload), visible=False)
        self.search = top.add(SearchBox(top, placeholder="Find a table or column…",
                                        on_change=lambda t: self.on_search(),
                                        on_next=self.next_match, primary=True, width=30,
                                        tooltip="Find tables by name or by a linked column, "
                                                "as you type, in the view shown: Tables "
                                                "lists only them, Diagram marks them and "
                                                "opens their groups, All links lists their "
                                                "links. Enter: next match."))
        self.empty_var = tk.BooleanVar(value=False)
        self.empty_cb = top.add(ttk.Checkbutton(top, text="Show empty tables (0)",
                                                variable=self.empty_var,
                                                command=self.refresh), gap=10)
        ToolTip(self.empty_cb, "Tables without rows cannot confirm a link by its values: they "
                               "are hidden from the tables and the diagram unless ticked "
                               "(declared foreign keys to them are marked 'declared, no "
                               "rows').")
        # only in a case of several databases (shown by reload)
        self.cross_only_var = tk.BooleanVar(value=False)
        self.cross_only_cb = top.add(ttk.Checkbutton(top, text="Only links across databases",
                                                     variable=self.cross_only_var,
                                                     command=self.refresh), gap=8,
                                     visible=False)
        ToolTip(self.cross_only_cb, "Show only the links between two databases of the case "
                                    "(matched by value)")
        self.map_btn = top.add(ttk.Button(top, text="Map again", command=self.map_again),
                               visible=False, gap=10)
        ToolTip(self.map_btn, "Find and check the links again (mapping was stopped)")
        # one Export ▾, as on the other tabs
        self.export_btn = top.add(ttk.Button(top, text="Export ▾"), gap=10)
        ToolTip(self.export_btn, "Export the links listed (CSV), the diagram (SVG) or the "
                                 "Database Map")
        self.export_menu = tk.Menu(self, tearoff=0)
        self.export_menu.add_command(label="Links listed (CSV)…",
                                     command=self.export_csv_dialog)
        self.export_menu.add_command(label="Diagram (SVG)…", command=self.export_svg_dialog)
        self.export_menu.add_separator()
        self.export_menu.add_command(label="Database Map…",
                                     command=lambda: self.app.datamap.export_map())
        self.export_btn.configure(command=self._post_export_menu)
        self.status = wrap_to_width(ttk.Label(self, text="Open a database to map its "
                                                         "relationships.", style="M.TLabel"))
        self.status.pack(fill="x", padx=8)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.on_search())
        self._build_tables()
        self._build_diagram()
        self._build_list()

    def _build_tables(self):
        tf = self.tables_frame = ttk.Frame(self.nb)
        self.nb.add(tf, text="  Tables  ")
        pane = ttk.PanedWindow(tf, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        cols = (("rows", "Rows", 80), ("refers", "Refers to", 70),
                ("referred", "Referred by", 80), ("across", "Across databases", 110))
        self.table_tree = ttk.Treeview(left, columns=[c for c, _t, _w in cols],
                                       show="tree headings", selectmode="browse")
        self.table_tree.heading("#0", text="Table")
        self.table_tree.column("#0", width=210, stretch=True)
        for c, title, w in cols:
            self.table_tree.heading(c, text=title)
            self.table_tree.column(c, width=w, stretch=False, anchor="e")
        self.table_tree.configure(displaycolumns=["rows", "refers", "referred"])
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.table_tree.yview)
        self.table_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.table_tree.pack(fill="both", expand=True)
        self.table_tree.tag_configure("match", background=C["hl"])
        self.table_tree.bind("<<TreeviewSelect>>", lambda e: self._table_selected())
        self.table_tree.bind("<Double-1>", lambda e: self.focus and self.browse_node(self.focus))
        TreeviewTooltip(self.table_tree)
        pane.add(left, weight=1)
        card = self.card = ttk.Frame(pane)
        pane.add(card, weight=3)
        self.card_title = ttk.Label(card, text="", style="B.TLabel")
        self.card_title.pack(fill="x", padx=8, pady=(6, 0))
        self.card_info = wrap_to_width(ttk.Label(card, text="", style="M.TLabel"))
        self.card_info.pack(fill="x", padx=8)
        bar = FlowFrame(card)
        bar.pack(fill="x", padx=8, pady=(4, 2))
        self.card_search = bar.add(SearchBox(bar, placeholder="Find in this table's links…",
                                             on_change=lambda t: self.fill_card(),
                                             on_next=self._card_next, width=26))
        self.card_btns = []
        for text, cmd, tip in (("Browse", self._card_browse,
                                "Open the selected linked table in Browse (this table when "
                                "no line is selected)"),
                               ("Column relationships…", self._card_columns,
                                "Every column related to this table's column of the "
                                "selected link"),
                               ("Show in diagram", self._card_diagram,
                                "Show this table and the tables linked to it in the "
                                "Diagram")):
            b = bar.add(ttk.Button(bar, text=text, command=cmd), gap=6)
            ToolTip(b, tip)
            self.card_btns.append(b)
        body = ttk.Frame(card)
        body.pack(fill="both", expand=True, padx=8, pady=2)
        self.card_tree = ttk.Treeview(body, columns=("columns", "strength", "evidence",
                                                     "rows"), show="tree headings",
                                      selectmode="browse")
        for c, title, w, st in (("#0", "Table", 240, True), ("columns", "Columns", 250, True),
                                ("strength", "Strength", 80, False),
                                ("evidence", "Values found", 150, False),
                                ("rows", "Rows", 90, False)):
            self.card_tree.heading(c, text=title)
            self.card_tree.column(c, width=w, stretch=st, anchor="e" if c == "rows" else "w")
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.card_tree.yview)
        self.card_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.card_tree.pack(fill="both", expand=True)
        self.card_tree.tag_configure("section", font=FONT["body_bold"],
                                     background=C["bg3"])
        self.card_tree.tag_configure("group", foreground=C["text2"],
                                     font=FONT["italic"])
        self.card_tree.tag_configure("weak", foreground=C["text2"])
        self.card_tree.tag_configure("cross", foreground=C["orange"])
        self.card_tree.tag_configure("match", background=C["hl"])
        self.card_tree.bind("<Double-1>", lambda e: self._card_browse())
        TreeviewTooltip(self.card_tree)
        ToolTip(self.card_tree, "Strength: Declared (a FOREIGN KEY in the schema), Strong "
                                "(values found for at least 95% of the sample), Likely "
                                "(other checked links), Weak (too few values, or a table "
                                "without rows)")

    def _build_diagram(self):
        df = self.diagram_frame = ttk.Frame(self.nb)
        self.nb.add(df, text="  Diagram  ")
        dbar = self.diagram_bar = FlowFrame(df)
        dbar.pack(fill="x", pady=2)
        dbar.add(ttk.Label(dbar, text="Table:"))
        self.focus_label = dbar.add(ttk.Label(dbar, text="", style="B.TLabel"))
        # what the diagram shows, as one segmented control: the chosen view is filled in
        # (a plain button gave no sign of which one was on)
        self.hops_var = tk.IntVar(value=1)
        self.view_var = tk.StringVar(value="1")
        for value, text, tip, gap in (
                ("1", "1 link away", "The selected table and the tables linked to it", 8),
                ("2", "2 links away", "The selected table, the tables linked to it, and the "
                                      "tables linked to those", 0),
                ("all", "Whole database", "Every linked table of the database (tables linked "
                                          "alike grouped)", 0)):
            rb = dbar.add(ttk.Radiobutton(dbar, text=text, value=value, variable=self.view_var,
                                          style="Segment.TRadiobutton",
                                          command=self._view_chosen), gap=gap)
            ToolTip(rb, tip)
            if value == "all":
                self.overview_btn = rb
        for text, cmd, tip in (("Fit", self.fit, "Zoom to show the whole diagram (key 0)"),
                               ("−", lambda: self.zoom_center(1 / 1.25), "Zoom out (key -)"),
                               ("+", lambda: self.zoom_center(1.25), "Zoom in (key +)")):
            b = dbar.add(ttk.Button(dbar, text=text, command=cmd,
                                    width=3 if len(text) == 1 else None),
                         gap=10 if text == "Fit" else 2)
            ToolTip(b, tip)
        self.tidy_btn = dbar.add(ttk.Button(dbar, text="Tidy", command=self.tidy), gap=10)
        ToolTip(self.tidy_btn, "Keep the cards where you moved them, but remove overlaps and "
                               "align them")
        self.arrange_btn = dbar.add(ttk.Button(dbar, text="Auto-arrange",
                                               command=self.auto_arrange), gap=2)
        ToolTip(self.arrange_btn, "Lay the diagram out again (forgets the cards you moved)")
        self.linked_only_var = tk.BooleanVar(value=False)
        cb = dbar.add(ttk.Checkbutton(dbar, text="Linked columns only",
                                      variable=self.linked_only_var,
                                      command=self._linked_only_changed), gap=10)
        ToolTip(cb, "Cards list only their key and linked columns (and how many others they "
                    "have). The ▸ / ▾ in a card's header does it for one card.")
        self.junction_var = tk.BooleanVar(value=False)
        cb = dbar.add(ttk.Checkbutton(dbar, text="N:M", variable=self.junction_var,
                                      command=self.draw), gap=6)
        ToolTip(cb, "Draw junction tables (a table whose columns refer to two or more tables, "
                    "with few other columns and nothing referring to it) as many-to-many "
                    "relationships between the tables they join")
        self.more_btn, self.more_menu = menu_button(dbar, "More ▾")
        dbar.add(self.more_btn, gap=10)
        self.more_menu.add_command(label="Export diagram (SVG)…",
                                   command=self.export_svg_dialog)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="List every group's tables",
                                   command=lambda: self.expand_groups(True))
        self.more_menu.add_command(label="Fold every group",
                                   command=lambda: self.expand_groups(False))
        self.minimap_var = tk.BooleanVar(value=True)
        self.more_menu.add_checkbutton(label="Minimap", variable=self.minimap_var,
                                       command=lambda: self.erd.show_minimap(
                                           self.minimap_var.get()))
        self.diagram_note = wrap_to_width(ttk.Label(df, text="", style="M.TLabel"))
        self.diagram_note.pack(fill="x", padx=4)
        self.hub_btn = ttk.Button(df, text="", command=self._focus_hub)
        leg = self.legend_frame = FlowFrame(df)
        leg.pack(fill="x", padx=4, side="bottom")
        self.legend = wrap_to_width(ttk.Label(df, text=self.LEGEND, style="M.TLabel"))
        self.legend.pack(fill="x", padx=4, side="bottom", before=leg)
        self._legend_items = {}
        for key, text in (("declared", "declared foreign key"),
                          ("verified", "verified by values"),
                          ("cross", "between databases (matched by value)"),
                          ("nm", "many-to-many (junction table)"),
                          ("many", "many"), ("one", "one"),
                          ("group", "group: tables linked alike")):
            f = ttk.Frame(leg)
            sw = tk.Canvas(f, width=30, height=14, bg=COLOR["background"],
                           highlightthickness=0)
            if key == "group":
                sw.create_rectangle(4, 2, 26, 12, fill=COLOR["card"], outline=COLOR["border"])
                sw.create_rectangle(4, 2, 26, 6, fill=COLOR["muted"], outline=COLOR["border"])
            elif key == "many":
                sw.create_line(2, 7, 28, 7, fill=COLOR["primary"], width=1.5)
                sw.create_line(28, 1, 17, 7, 28, 13, fill=COLOR["primary"], width=1.5)
            elif key == "one":
                sw.create_line(2, 7, 28, 7, fill=COLOR["primary"], width=1.5)
                sw.create_line(21, 1, 21, 13, fill=COLOR["primary"], width=1.5)
            else:
                style = {"declared": "declared", "verified": "verified", "cross": "cross",
                         "nm": "nm"}[key]
                sw.create_line(2, 7, 28, 7, fill=COLOR[LINE_COLORS[style]], width=1.8,
                               dash=erd.STYLES[style][1])
            sw.pack(side="left")
            ttk.Label(f, text=text, style="M.TLabel").pack(side="left")
            self._legend_items[key] = leg.add(f, gap=12)
        leg.show(self._legend_items["cross"], False)
        leg.show(self._legend_items["nm"], False)
        body = ttk.Frame(df)
        body.pack(fill="both", expand=True)
        self.side = ttk.Frame(body, width=340)
        self.erd = ErdCanvas(body, on_table=self._erd_table, on_relationship=self._erd_rel,
                             on_open=self.browse_node, on_positions=self._erd_positions,
                             on_group=self._erd_group)
        self.erd.pack(side="left", fill="both", expand=True)
        self.canvas = self.erd.canvas
        self._build_side()

    def _build_side(self):
        s = self.side
        head = ttk.Frame(s)
        head.pack(fill="x", padx=6, pady=(6, 2))
        self.side_title = ttk.Label(head, text="", style="B.TLabel", wraplength=300)
        self.side_title.pack(side="left", fill="x", expand=True)
        close = ttk.Button(head, text="×", width=2, style="Sm.TButton",
                           command=lambda: self.select(None))
        close.pack(side="right")
        ToolTip(close, "Close the panel")
        self.side_info = ttk.Label(s, text="", style="M.TLabel", wraplength=320,
                                   justify="left")
        self.side_info.pack(fill="x", padx=6)
        btns = self.side_bar = FlowFrame(s)
        btns.pack(fill="x", padx=6, pady=6, side="bottom")
        # a table's or a group's links
        lst = self.side_list = ttk.Frame(s)
        lst.pack(fill="both", expand=True)
        self.side_search = SearchBox(lst, placeholder="Find…", on_change=lambda t: self._fill_side(),
                                     on_next=lambda f: None, find_button=False, width=20)
        self.side_search.pack(fill="x", padx=6, pady=2)
        box = ttk.Frame(lst)
        box.pack(fill="both", expand=True, padx=6, pady=2)
        self.side_tree = ttk.Treeview(box, columns=("way", "other", "columns", "kind", "found"),
                                      show="headings", selectmode="browse", height=12)
        for c, title, w in (("way", "", 28), ("other", "Table", 120), ("columns", "Columns", 150),
                            ("kind", "Link", 110), ("found", "Values found", 90)):
            self.side_tree.heading(c, text=title)
            self.side_tree.column(c, width=w, stretch=c in ("other", "columns"), anchor="w")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.side_tree.yview)
        self.side_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.side_tree.pack(fill="both", expand=True)
        self.side_tree.tag_configure("cross", foreground=C["orange"])
        self.side_tree.bind("<Double-1>", lambda e: self._side_browse())
        TreeviewTooltip(self.side_tree)
        # a relationship: cardinality, evidence, a sample JOIN
        rb = self.rel_box = ttk.Frame(s)
        self.rel_card = ttk.Label(rb, text="", wraplength=320, justify="left")
        self.rel_card.pack(fill="x", padx=6, pady=(6, 2))
        self.rel_why = ttk.Label(rb, text="", style="M.TLabel", wraplength=320, justify="left")
        self.rel_why.pack(fill="x", padx=6, pady=2)
        ttk.Label(rb, text="Sample JOIN", style="B.TLabel").pack(fill="x", padx=6,
                                                                 pady=(8, 2))
        self.sql_text = tk.Text(rb, height=8, width=40, wrap="none", font=FONT["mono"],
                                relief="flat", bg=COLOR["card"], fg=COLOR["text"],
                                highlightthickness=1, highlightbackground=COLOR["border"],
                                padx=6, pady=4)
        self.sql_text.pack(fill="both", expand=True, padx=6, pady=2)
        self.sql_text.configure(state="disabled")
        self.side_btns = {}
        for key, text, tip in (("browse", "Browse", "Open the table in Browse (the selected "
                                                    "line's table, if any)"),
                               ("browse_src", "Browse source", "Open the referring table in "
                                                               "Browse"),
                               ("browse_dst", "Browse target", "Open the referred table in "
                                                               "Browse"),
                               ("copy_sql", "Copy SQL", "Copy the sample JOIN"),
                               ("related", "Column relationships…",
                                "Every column related to the selected link's column"),
                               ("focus", "Focus on this table",
                                "Put this table in the middle of the diagram"),
                               ("expand", "Collapse", "List the group's tables one by one, "
                                                      "or fold them into one card")):
            b = btns.add(ttk.Button(btns, text=text, command=lambda k=key: self._side_action(k)),
                         gap=4)
            ToolTip(b, tip)
            self.side_btns[key] = b
        self._side_rows = {}

    def _build_list(self):
        lf = self.list_frame = ttk.Frame(self.nb)
        self.nb.add(lf, text="  All links  ")
        bar = self.filter_bar = FlowFrame(lf)
        bar.pack(fill="x", pady=2)
        self.weaker_var = tk.BooleanVar(value=False)
        self.weaker_cb = bar.add(ttk.Checkbutton(bar, text="Include weaker links",
                                                 variable=self.weaker_var,
                                                 command=self.refresh))
        ToolTip(self.weaker_cb, "Also list links whose names or values only partly agree "
                                "(with the reason)")
        self.only_table_var = tk.BooleanVar(value=False)
        self.only_table_cb = bar.add(ttk.Checkbutton(bar, text="Only the selected table",
                                                     variable=self.only_table_var,
                                                     command=self.refresh), gap=8)
        ToolTip(self.only_table_cb, "List only the links of the table selected in Tables")
        btns = ttk.Frame(lf)
        btns.pack(fill="x", pady=4, side="bottom")
        for text, cmd, tip in (("Browse source column", lambda: self.browse_selected(False),
                                "Open the linking table in Browse"),
                               ("Browse target", lambda: self.browse_selected(True),
                                "Open the linked table in Browse"),
                               ("Column relationships…", self.open_selected,
                                "Every column related to the link's column")):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side="left", padx=3)
            ToolTip(b, tip)
        box = ttk.Frame(lf)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=[c for c, _t, _w in self.LIST_COLUMNS],
                                 show="headings", selectmode="browse")
        for c, title, w in self.LIST_COLUMNS:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, stretch=w >= 300, anchor="w")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("declared", foreground=C["accent"])
        self.tree.tag_configure("verified", foreground=C["green"])
        self.tree.tag_configure("weaker", foreground=C["text2"])
        self.tree.tag_configure("value", foreground=C["orange"])
        self.tree.configure(displaycolumns=[c for c, _t, _w in self.columns()])
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        TreeviewTooltip(self.tree)

    def view(self):
        """'tables', 'diagram' or 'list': the view shown."""
        try:
            cur = self.nb.select()
        except tk.TclError:
            return "tables"
        if cur == str(self.diagram_frame):
            return "diagram"
        if cur == str(self.list_frame):
            return "list"
        return "tables"

    def show_view(self, name):
        self.nb.select({"tables": self.tables_frame, "diagram": self.diagram_frame,
                        "list": self.list_frame}[name])
        self.update_idletasks()
        self.on_search()

    # -- data ----------------------------------------------------------------------------------
    def on_state(self, state):
        """RelationWindows mapping progress: ('mapping', done, total) ... ('done', ...)."""
        kind, done, total = state
        if kind == "idle":
            self.reset()
        elif kind == "mapping":
            self.status.configure(text="Mapping… %d/%d tables" % (done, total))
            self.top_bar.show(self.map_btn, False)
        else:
            self.reload()
            if kind == "stopped":
                self.status.configure(text=self.status.cget("text") + " (mapping stopped: "
                                                                      "values partly checked)")
                self.top_bar.show(self.map_btn, True)

    def map_again(self):
        self.top_bar.show(self.map_btn, False)
        self.manager.start_mapping()

    def reset(self):
        self.all_links, self.shown_links = [], []
        self.graph = TabGraph([], self.erd)
        self.model = None
        self.focus = None
        self.overview = False
        self._expanded = set()
        self._search_groups = set()
        self._matches = []
        self._selected = None
        self._spec_cache = {}
        self.tree.delete(*self.tree.get_children())
        self.table_tree.delete(*self.table_tree.get_children())
        self.card_tree.delete(*self.card_tree.get_children())
        self.card_title.configure(text="")
        self.card_info.configure(text="")
        self.erd.clear()
        self.side.pack_forget()
        self.diagram_note.configure(text="")
        self.focus_label.configure(text="")
        self.status.configure(text="Open a database to map its relationships.")

    def reload(self):
        """Read the links from the maps (after mapping): every database's, and in a case the
        links between databases."""
        members = [m for m in self.manager.members() if m.db.session is not None]
        if not members:
            self.reset()
            return
        multi = self.manager.multi()
        # in a case, the databases the Relationships scope covers
        scopes = getattr(self.app, "scopes", None)
        picker = getattr(self, "scope_picker", None)
        if picker is not None:
            self.top_bar.show(picker, multi)
            picker.refresh()
        if multi and scopes is not None:
            chosen = set(m.uid for m in scopes.members("relations"))
            members = [m for m in members if m.uid in chosen] or members
        if multi != self._multi:
            self._multi = multi
            self.tree.configure(displaycolumns=[c for c, _t, _w in self.columns()])
            self.table_tree.configure(displaycolumns=["rows", "refers", "referred"] +
                                      (["across"] if multi else []))
            self.top_bar.show(self.cross_only_cb, multi)
            self.legend_frame.show(self._legend_items["cross"], multi)
            if not multi:
                self.cross_only_var.set(False)
        self._members = {}
        links = []
        for m in members:
            key = m.name if multi else DB
            self._members[key] = m
            mp = self.manager.map_of(m)
            links.extend(Link(r, key, rows=mp.known_rows) for r in mp.links())
        if multi:
            names = dict((m.uid, m.name) for m in members)
            maps = dict((m.uid, self.manager.map_of(m)) for m in members)
            links.extend(ValueLink(l, names, lambda db, t: maps[db].known_rows(t))
                         for l in self.manager.cross_links(confident=False)
                         if l.src_db in names and l.dst_db in names)
        self.all_links = links
        self.focus = None
        self._expanded = set()
        self._selected = None
        self._spec_cache = {}
        self.refresh()

    def _table_label(self, node):
        return node_label(node)

    def summary(self):
        conf = [l for l in self.all_links if l.confident]
        declared = sum(1 for l in conf if l.kind == "declared")
        cross = sum(1 for l in conf if l.cross)
        weaker = len(self.all_links) - len(conf)
        text = "%d link%s (%d declared, %d verified by values)" % (
            len(conf), "" if len(conf) == 1 else "s", declared, len(conf) - declared - cross)
        details = []        # said in the line's tooltip: the line stays short at any size
        if self._multi:
            res = self.manager.cross
            text = "%d databases: " % len(self._members) + text[:-1] + \
                ", %d between databases)" % cross
            if res is not None and not cross:
                n_ints = sum(res.int_profiles.values())
                details.append("No link between the databases was found (%d text column%s "
                               "and %d integer id column%s compared)." % (
                                   sum(res.profiles.values()),
                                   "" if sum(res.profiles.values()) == 1 else "s",
                                   n_ints, "" if n_ints == 1 else "s"))
            if res is not None:
                details.append("Links between databases are matched by value; " +
                               res.limits_text() + ".")
        if weaker:
            text += ", %d weaker" % weaker
        # P1-9: 'between 0 tables' contradicts itself; count all mapped tables
        n_tables = len(self.graph.nodes)
        if n_tables == 0:
            try:
                n_tables = sum(len(m.db.tables()) for m in self._members.values()
                             if m is not None and m.db is not None)
            except Exception:
                pass
        text += " across %d table%s" % (n_tables,
                                         "" if n_tables == 1 else "s")
        hidden = self._empty_count()
        if hidden and not self.empty_var.get():
            text += " (%d empty table%s hidden)" % (hidden, "" if hidden == 1 else "s")
        secs = self.manager.map_seconds
        if secs is not None:
            details.append("Mapped in %.1f s." % secs)
        self.summary_details = "\n".join(details)
        return text

    def _scope_links(self):
        """The links of the tables and the diagram: across databases only when asked, and
        without the tables that have no rows unless 'Show empty tables' is ticked."""
        links = self.all_links
        if self._multi and self.cross_only_var.get():
            links = [l for l in links if l.cross]
        if not self.empty_var.get():
            links = [l for l in links if not l.empty()]
        return links

    def _empty_count(self):
        empty = set()
        for l in self.all_links:
            if l.src_rows == 0:
                empty.add(l.src)
            if l.dst_rows == 0:
                empty.add(l.dst)
        return len(empty)

    def refresh(self):
        """Rebuild everything from the links (after mapping or a change of the options)."""
        self._rows = {}
        for l in self.all_links:
            self._rows[l.src] = l.src_rows
            self._rows[l.dst] = l.dst_rows
        self.empty_cb.configure(text="Show empty tables (%d)" % self._empty_count())
        self.graph = TabGraph(self._scope_links(), self.erd)
        if self.focus is None or self.focus not in self.graph.nodes:
            self.focus = self._default_focus()
        self._selected = None
        self.fill_list()
        self.fill_tables()
        self.fill_card()
        self.draw()
        if self.all_links or self.manager.state[0] in ("done", "stopped"):
            self.status.configure(text=self.summary())
            tip = getattr(self, "_status_tip", None)
            if tip is None:
                tip = self._status_tip = ToolTip(self.status, "")
            tip.text = self.summary_details or self.status.cget("text")
        self._update_count()

    def _default_focus(self):
        browsing = self.app._browse_table_var.get() if hasattr(self.app, "_browse_table_var") \
            else ""
        active = self.manager.active()
        key = active.name if self._multi and active is not None else DB
        if (key, browsing) in self.graph.nodes:
            return (key, browsing)
        return self.graph.most_linked()

    def _focus_key(self, name):
        if name in ("", ALL):
            return ALL
        for n in self.graph.nodes:
            if node_label(n) == name:
                return n
        return (DB, name)

    def set_focus(self, focus):
        """Select a table (ALL: the Diagram's overview) in every view."""
        if focus == ALL or focus is None:
            self.overview = True
            self.draw()
            return
        self.overview = False
        self.focus = focus
        self._selected = None
        self._select_in_table_list(focus)
        self.fill_card()
        self.draw()
        if self.only_table_var.get():
            self.fill_list()

    # -- search --------------------------------------------------------------------------------
    def term(self):
        return self.search.get().lower()

    def on_search(self):
        """The search changed or another view was shown: apply it to the view shown."""
        v = self.view()
        if v == "tables":
            self.fill_tables()
        elif v == "list":
            self.fill_list()
        else:
            if self.model is None:
                self.draw()
            else:
                self._apply_search()
            if self.term():
                self.next_match(True)
        self._update_count()

    def _update_count(self):
        v, term = self.view(), self.term()
        if not term:
            self.search.set_count(0, 0)
            return
        if v == "tables":
            self.search.set_count(len(self._table_iids), len(self._listable()), "table",
                                  "tables", "table or column")
        elif v == "list":
            base = self._list_links(term="")
            self.search.set_count(len(self.shown_links), len(base), "link", "links",
                                  "link, table or column")
        else:
            total = self.model.table_count() if self.model is not None else 0
            self.search.set_count(len(self._matches), total, "table", "tables",
                                  "table or column")

    def next_match(self, forward=True):
        v = self.view()
        if v == "tables":
            _move_selection(self.table_tree, forward)
        elif v == "list":
            _move_selection(self.tree, forward)
        else:
            matches = self._matches
            if not matches:
                return
            self._match_i = (self._match_i + (1 if forward else -1)) % len(matches)
            n = matches[self._match_i]
            self.see_node(n)
            box = self._box_of(n)
            if box is not None:
                self.select(box, panel=False)     # its links stand out; no panel pops up

    # -- Tables view ---------------------------------------------------------------------------
    def _listable(self):
        return sorted(self.graph.nodes, key=lambda n: (node_label(n).lower(), n))

    def _counts(self, node):
        refers, referred, across = set(), set(), 0
        for l in self.graph.adj.get(node, ()):
            if l.cross:
                across += 1
            if l.direction == "peer":
                continue
            if l.src == node and l.dst != node:
                refers.add(l.dst)
            elif l.dst == node and l.src != node:
                referred.add(l.src)
        return len(refers), len(referred), across

    def fill_tables(self):
        tree = self.table_tree
        tree.delete(*tree.get_children())
        self._table_iids = {}
        term = self.term()
        for n in self._listable():
            if term and not self.graph.matches(n, term):
                continue
            refers, referred, across = self._counts(n)
            rows = self._rows.get(n)
            iid = tree.insert("", "end", text=node_label(n), values=(
                "?" if rows is None else format(rows, ","), refers, referred,
                across or ""), tags=("match",) if term else ())
            self._table_iids[iid] = n
        if self.focus is not None:
            self._select_in_table_list(self.focus)

    def _select_in_table_list(self, node):
        for iid, n in self._table_iids.items():
            if n == node:
                if tuple(self.table_tree.selection()) != (iid,):
                    self._quiet = True
                    self.table_tree.selection_set(iid)
                self.table_tree.see(iid)
                return

    def _table_selected(self):
        if getattr(self, "_quiet", False):
            self._quiet = False
            return
        sel = self.table_tree.selection()
        n = self._table_iids.get(sel[0]) if sel else None
        if n is not None and n != self.focus:
            self.set_focus(n)

    def card_links(self, node=None):
        """(refers to, referred by, same values, weaker) links of a table."""
        node = node or self.focus
        out, inc, peer = [], [], []
        for l in self.graph.adj.get(node, ()):
            if l.direction == "peer":
                peer.append(l)
            elif l.src == node:
                out.append(l)
            else:
                inc.append(l)
        weak = [l for l in self.all_links if not l.confident and node in (l.src, l.dst) and
                (not (self._multi and self.cross_only_var.get()) or l.cross)]
        return out, inc, peer, weak

    def fill_card(self):
        tree = self.card_tree
        tree.delete(*tree.get_children())
        self._card_rows = {}
        node = self.focus
        if node is None:
            self.card_title.configure(text="")
            self.card_info.configure(text="No table has trusted links." if self.all_links
                                     else "")
            self.card_search.set_count(0, 0)
            return
        out, inc, peer, weak = self.card_links(node)
        refers, referred, _a = self._counts(node)
        rows = self._rows.get(node)
        self.card_title.configure(text=node_label(node))
        info = "Referred by %d table%s, refers to %d" % (referred, "" if referred == 1 else "s",
                                                         refers)
        if peer:
            info += ", shares values with %d" % len(set(l.dst if l.src == node else l.src
                                                        for l in peer))
        info += " — %s row%s" % ("?" if rows is None else format(rows, ","),
                                 "" if rows == 1 else "s")
        if rows == 0:
            info += " (empty: its links cannot be checked against values)"
        self.card_info.configure(text=info)
        term = self.card_search.get().lower()
        shown = [0, 0]

        def hit(l):
            return not term or any(term in s.lower() for s in (
                node_label(l.src), node_label(l.dst), col_name(l.src_col), col_name(l.dst_col)))

        def other(l):
            return l.dst if l.src == node else l.src

        def add(parent, l, tags=()):
            o = other(l)
            vals = ("%s → %s" % (col_name(l.src_col), col_name(l.dst_col)) if l.direction !=
                    "peer" else "%s = %s" % (col_name(l.src_col), col_name(l.dst_col)),
                    l.strength(), l.evidence() or l.kind_text(),
                    "?" if self._rows.get(o) is None else format(self._rows[o], ","))
            if l.kind != "declared" and not l.confident:
                vals = (vals[0], vals[1], l.reason[:120], vals[3])
            elif l.kind == "declared" and l.empty():
                vals = (vals[0], vals[1], "declared, no rows", vals[3])
            iid = tree.insert(parent, "end", text=node_label(o), values=vals,
                              tags=tags + (("cross",) if l.cross else ()) +
                              (("match",) if term else ()))
            self._card_rows[iid] = l
            return iid

        def section(title, links, group=False, open_=True, tags=()):
            links = sorted(links, key=lambda l: (_strength_sort(l), node_label(other(l)).lower(),
                                                 col_name(l.src_col)))
            match = [l for l in links if hit(l)]
            shown[0] += len(match)
            shown[1] += len(links)
            if not links:
                return
            head = tree.insert("", "end", text="%s (%d)" % (title, len(links)), open=open_,
                               tags=("section",))
            if term and not match:
                tree.insert(head, "end", text="(none matches “%s”)" % term, tags=("weak",))
                return
            if not group:
                for l in match:
                    add(head, l, tags)
                return
            buckets = {}
            for l in match:
                key = (col_name(l.src_col), col_name(l.dst_col), l.kind, l.cross)
                buckets.setdefault(key, []).append(l)
            minimum = limits.get("diagram_group_min")
            singles = []
            for key in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
                ls = buckets[key]
                if len(ls) < minimum:
                    singles.extend(ls)
                    continue
                g = tree.insert(head, "end", text="%d tables via %s → %s" % (len(ls), key[0],
                                                                            key[1]),
                                values=("", ls[0].strength(), "", ""), open=bool(term),
                                tags=("group",))
                for l in ls:
                    add(g, l)
            for l in sorted(singles, key=lambda l: (_strength_sort(l),
                                                    node_label(other(l)).lower())):
                add(head, l)
        section("Refers to", out)
        section("Referred by", inc, group=True)
        section("Shares values with", peer)
        section("Weaker links", weak, open_=bool(term), tags=("weak",))
        self.card_search.set_count(shown[0], shown[1], "link", "links", "link")

    def _card_selected(self):
        sel = self.card_tree.selection()
        return self._card_rows.get(sel[0]) if sel else None

    def _card_next(self, forward):
        _move_selection(self.card_tree, forward, lambda iid: iid in self._card_rows)

    def _card_browse(self):
        l = self._card_selected()
        if l is None:
            if self.focus is not None:
                self.browse_node(self.focus)
            return
        self.browse_node(l.dst if l.src == self.focus else l.src)

    def _card_columns(self):
        l = self._card_selected()
        if l is None or self.focus is None:
            return
        col = l.src_col if l.src == self.focus else l.dst_col
        if l.cross:
            return
        self.manager.column_map(self.focus[1], col, self.member_of(self.focus))

    def _card_diagram(self):
        l = self._card_selected()
        self.overview = False
        self.show_view("diagram")
        if l is not None:
            o = l.dst if l.src == self.focus else l.src
            box = self._box_of(o)
            if box is not None:
                self.select(box)
                self.see_key(box[1])

    # -- All links view --------------------------------------------------------------------------
    def _list_links(self, term=None):
        weaker = self.weaker_var.get()
        term = self.term() if term is None else term
        links = [l for l in self.all_links if weaker or l.confident]
        if self._multi and self.cross_only_var.get():
            links = [l for l in links if l.cross]
        if not self.empty_var.get():
            links = [l for l in links if not l.empty() or not l.confident]
        if self.only_table_var.get() and self.focus is not None:
            links = [l for l in links if self.focus in (l.src, l.dst)]
        if term:
            links = [l for l in links if any(term in str(v).lower() for v in l.row())]
        return links

    def fill_list(self):
        self.shown_links = sorted(self._list_links(),
                                  key=lambda l: (-l.confident, -l.score, l.row()))
        self.tree.delete(*self.tree.get_children())
        for i, l in enumerate(self.shown_links):
            self.tree.insert("", "end", iid=str(i), values=l.row(),
                             tags=("value" if l.cross and l.confident else l.kind,))
        self.only_table_cb.configure(text="Only %s" % node_label(self.focus)
                                     if self.focus is not None else "Only the selected table")

    def list_rows(self):
        return [tuple(self.tree.item(i, "values"))[:len(self.columns())]
                for i in self.tree.get_children()]

    def selected(self):
        sel = self.tree.selection()
        return self.shown_links[int(sel[0])] if sel else None

    def member_of(self, node):
        """The case member a diagram node's database is (None: the App's one database)."""
        return self._members.get(node[0])

    def open_selected(self):
        l = self.selected()
        if l is not None and not l.cross:
            self.manager.column_map(l.src[1], l.src_col, self.member_of(l.src))

    def browse_node(self, node):
        """Browse a table (its database made active first, in a case)."""
        m = self.member_of(node)
        activate = getattr(self.app, "activate_member", None)
        if m is not None and m.uid is not None and activate is not None:
            activate(m)
        self.app.browse_table(node[1])

    def browse_selected(self, target):
        l = self.selected()
        if l is not None:
            self.browse_node(l.dst if target else l.src)

    # -- diagram -------------------------------------------------------------------------------
    def toggle_overview(self):
        self.overview = not self.overview
        self._selected = None
        self.draw()

    def _view_chosen(self):
        """The segmented control: 1 or 2 links away from the selected table, or the whole
        database."""
        v = self.view_var.get()
        if v == "all":
            self.overview = True
        else:
            self.overview = False
            self.hops_var.set(int(v))
        self._selected = None
        self.draw()

    def _sync_view_var(self):
        want = "all" if self.overview else str(self.hops_var.get())
        if self.view_var.get() != want:
            self.view_var.set(want)

    def _focus_hub(self):
        hub = self.graph.most_linked()
        if hub is not None:
            self.set_focus(hub)

    def _diagram_links(self, focus):
        """(links, tables kept as cards) of the diagram: the whole database, or the focused
        table and the tables 1 or 2 links away."""
        g = self.graph
        if focus is None or focus not in g.nodes:
            return list(g.links), ()
        shown = set([focus]) | g.neighbours(focus)
        if self.hops_var.get() >= 2:
            for n in list(shown):
                shown |= g.neighbours(n)
        return [l for l in g.links if l.src in shown and l.dst in shown], (focus,)

    def _specs(self, nodes):
        """{node: engine.erd.TableSpec} from each database's schema (cached until reload)."""
        want = {}
        for n in nodes:
            if n not in self._spec_cache:
                want.setdefault(n[0], []).append(n[1])
        for key, tables in want.items():
            m = self._members.get(key)
            got = {}
            if m is not None:
                try:
                    mp = self.manager.map_of(m)
                    got = erd.table_specs(mp.session, sorted(set(tables)), mp.known_rows)
                except Exception:           # noqa: BLE001 - columns then come from the links
                    got = {}
            for t in tables:
                self._spec_cache[(key, t)] = got.get(t)
        return dict((n, self._spec_cache[n]) for n in nodes if self._spec_cache.get(n))

    def _in_view(self):
        """False while another tab of the app's notebook is the one shown."""
        try:
            return _in_view(self, self.winfo_toplevel())
        except tk.TclError:
            return True

    def _draw_if_pending(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._draw_pending and self._in_view():
            self.draw()

    def draw(self):
        """Build the diagram's model from the links in scope and draw it (once the tab is in
        view: drawing a large diagram nobody sees held the Tk thread when the links of a case
        were checked)."""
        if not self._in_view():
            self._draw_pending = True
            return
        self._draw_pending = False
        focus = None if self.overview else self.focus
        links, keep = self._diagram_links(focus)
        nodes = set(keep)
        for l in links:
            nodes.add(l.src)
            nodes.add(l.dst)
        self.model = erd.ErdModel(links, self._specs(nodes), keep=keep, tables=keep,
                                  junctions=self.junction_var.get())
        self._matches, groups, marks = self._find(self.term())
        self._search_groups = groups
        self.erd.set_expanded(self._expanded | groups)
        self.erd.show(self.model, self._saved_positions(), colors=self._colors(),
                      focus=focus, marks=marks)
        self._match_i = -1
        self.focus_label.configure(text=node_label(self.focus) if self.focus else "")
        self._sync_view_var()
        self.diagram_note.configure(text=self._note())
        self.legend_frame.show(self._legend_items["nm"], bool(self.junction_var.get()))
        if self.overview and self.graph.nodes:
            hub = self.graph.most_linked()
            self.hub_btn.configure(text="Focus on %s" % node_label(hub))
            if not self.hub_btn.winfo_manager():
                self.hub_btn.pack(anchor="w", padx=4, pady=(0, 2), after=self.diagram_note)
        else:
            self.hub_btn.pack_forget()
        if self._selected is not None and self._selected[0] == "rel":
            old = self._selected[1]      # the same relationship in the new model
            same = [r for r in self.model.rels if (r.src, r.src_col, r.dst, r.dst_col,
                                                   r.junction) == (old.src, old.src_col,
                                                                   old.dst, old.dst_col,
                                                                   old.junction)]
            self._selected = ("rel", same[0]) if same else None
        if self._selected is not None and not self._still_drawn(self._selected):
            self._selected = None
        if self._selected is None:
            self.side.pack_forget()
        else:
            self._sync_selection()
            if self.side.winfo_manager():
                self._fill_side()                   # the panel open stays, with fresh links

    def _still_drawn(self, hit):
        if hit[0] == "rel":
            return any(r is hit[1] for r in self.model.rels)
        if hit[0] == "group":
            return hit[1] in self.model.groups
        return self._box_of(hit[1]) is not None

    def _find(self, term):
        """(matching tables, groups holding one, card keys to mark) for Find's words: a
        table's name or one of its columns."""
        if not term or self.model is None:
            return [], set(), set()
        hits, groups, marks = [], set(), set()
        for key, t in self.model.tables.items():
            if term in t.label.lower() or any(term in c.name.lower() for c in t.columns):
                hits.append(key)
                marks.add(key)
        for gid, g in self.model.groups.items():
            for m, _rows in g.members:
                if self.graph.matches(m, term):
                    hits.append(m)
                    groups.add(gid)
                    marks.add(gid)
                    marks.add(m)
        return sorted(set(hits), key=lambda n: (node_label(n).lower(), n)), groups, marks

    def _apply_search(self):
        """Find's words changed in the Diagram: mark the tables (groups holding one list their
        tables), without laying the diagram out again unless a group opens or closes."""
        self._matches, groups, marks = self._find(self.term())
        if groups != self._search_groups:
            self._search_groups = groups
            self.erd.set_expanded(self._expanded | groups)
            self.erd.marks = set(marks)
            self.erd.relayout()
        self.erd.set_marks(marks)
        self._match_i = -1

    def _note(self):
        model = self.model
        if not self.graph.nodes or model is None or not model.cards():
            return "No trusted links to draw." if self.all_links else ""
        tables = model.table_count()
        stats = self.erd.layout.stats if self.erd.layout is not None else {}
        parts = []
        focus = None if self.overview else self.focus
        if focus is None:
            comps = stats.get("components", 1)
            parts.append("Overview: all %d linked tables in %d block%s of connected tables"
                         % (tables, comps, "" if comps == 1 else "s"))
        else:
            parts.append("%s and the %d table%s linked to it%s" % (
                node_label(focus), tables - 1, "" if tables == 2 else "s",
                " (and to those)" if self.hops_var.get() > 1 else ""))
        if model.groups:
            grouped = sum(len(g.members) for g in model.groups.values())
            parts.append("%d in %d group%s (click a group to list its tables)" % (
                grouped, len(model.groups), "" if len(model.groups) == 1 else "s"))
        if model.hidden_junctions:
            parts.append("%d junction table%s drawn as many-to-many: %s" % (
                len(model.hidden_junctions), "" if len(model.hidden_junctions) == 1 else "s",
                ", ".join(node_label(k) for k in model.hidden_junctions)))
        elif model.junctions:
            parts.append("%d junction table%s (N:M draws %s as many-to-many)" % (
                len(model.junctions), "" if len(model.junctions) == 1 else "s",
                "it" if len(model.junctions) == 1 else "them"))
        cut = model.cut_cards()
        if cut:
            parts.append("%d card%s list%s at most %d columns (limit diagram_card_columns; "
                         "click 'more columns' to list all)" % (
                             len(cut), "" if len(cut) == 1 else "s",
                             "s" if len(cut) == 1 else "", limits.get("diagram_card_columns")))
        if len(model.rels) > limits.get("diagram_edge_labels"):
            parts.append("column names of a line: hover it (%d lines, labelled up to %d: "
                         "limit diagram_edge_labels)" % (len(model.rels),
                                                         limits.get("diagram_edge_labels")))
        if focus is None:
            hub = self.graph.most_linked()
            parts.append("most linked: %s (%d links)" % (node_label(hub),
                                                         self.graph.degree(hub)))
        text = "; ".join(parts) + "."
        return text[0].upper() + text[1:]

    def node_items(self):
        """The canvas items naming a table card."""
        return self.canvas.find_withtag("name")

    def shown_tables(self):
        """The tables drawn as their own card."""
        if self.model is None:
            return []
        return sorted(n[1] for n in self.model.tables)

    def _box_of(self, node):
        """('node', node) when the table has its own card, ('group', gid) when it is in a
        group, None when not drawn."""
        if self.model is None:
            return None
        if node in self.model.tables:
            return ("node", node)
        g = self.model.group_of(node)
        return ("group", g.key) if g is not None else None

    def toggle_group(self, gid):
        """List a group's tables in its card, or fold them."""
        expand = gid not in self.erd.expanded_groups()
        self.erd.toggle_group(gid, expand)

    def expand_groups(self, expand):
        if self.model is None:
            return
        self._expanded = set(self.model.groups) if expand else set()
        self.erd.set_expanded(self._expanded | self._search_groups)
        self.erd.relayout()

    def _erd_group(self, gid, expanded):
        if expanded:
            self._expanded.add(gid)
        else:
            self._expanded.discard(gid)
            self._search_groups.discard(gid)
        if self._selected == ("group", gid) and self.side.winfo_manager():
            self._fill_side()

    def _linked_only_changed(self):
        self.erd.set_linked_only(self.linked_only_var.get())

    def tidy(self):
        self.erd.tidy()

    def auto_arrange(self):
        self.erd.auto_arrange()

    def _erd_table(self, key):
        """A card (or a table listed in a group) was clicked; None: the background."""
        if key is None:
            self.select(None)
        elif isinstance(key, tuple):
            self.select(("node", key))
        else:
            self.select(("group", key))

    def _erd_rel(self, rel):
        self.select(("rel", rel))

    # -- saved positions -----------------------------------------------------------------------
    def _view_key(self):
        focus = None if self.overview else self.focus
        key = "overview" if focus is None else "focus:%s:%s:%d" % (focus[0], focus[1],
                                                                   self.hops_var.get())
        return key + (":nm" if self.junction_var.get() else "")

    def _tags(self):
        tags = getattr(self.app, "tags", None)
        return tags if tags is not None and hasattr(tags, "member_section") else None

    def _saved_positions(self):
        """{card key: (x, y)} the user left the cards of this view at (in the case state of
        each database when there is one, else for this session)."""
        vk = self._view_key()
        tags = self._tags()
        if tags is None:
            return dict(self._positions.get(vk, {}))
        out = dict(self._positions.get(vk, {}))
        for key, m in self._members.items():
            try:
                sec = tags.member_section(m if self._multi else None, POSITIONS)
            except Exception:           # noqa: BLE001 - no saved state for this database
                continue
            for table, xy in ((sec or {}).get(vk) or {}).items():
                try:
                    out[(key, table)] = (float(xy[0]), float(xy[1]))
                except (TypeError, ValueError, IndexError):
                    continue
        return out

    def _erd_positions(self, positions):
        """Cards moved or tidied (keep their positions), or auto-arranged (None: forget)."""
        vk = self._view_key()
        tags = self._tags()
        tables = {}
        for k, xy in (positions or {}).items():
            if isinstance(k, tuple):
                tables.setdefault(k[0], {})[k[1]] = [round(xy[0], 1), round(xy[1], 1)]
        # kept for this session too (a database without saved state)
        if positions is None:
            self._positions.pop(vk, None)
        else:
            self._positions[vk] = dict((k, v) for k, v in positions.items()
                                       if isinstance(k, tuple))
        if tags is None:
            return
        for key, m in self._members.items():
            member = m if self._multi else None
            try:
                sec = dict(tags.member_section(member, POSITIONS) or {})
                if positions is None:
                    if vk not in sec:
                        continue
                    sec.pop(vk, None)
                else:
                    sec[vk] = tables.get(key, {})
                tags.set_member_section(member, POSITIONS, sec)
            except Exception:           # noqa: BLE001 - positions are a convenience
                continue

    # -- selection and the side panel ----------------------------------------------------------
    def _sync_selection(self):
        hit = self._selected
        if hit is None:
            self.erd.select_rel(None)
        elif hit[0] == "rel":
            self.erd.select_rel(hit[1].rid)
        elif hit[0] == "group":
            self.erd.select_card(hit[1])
        else:
            box = self._box_of(hit[1])
            self.erd.select_card(box[1] if box is not None else None)

    def select(self, hit, panel=True):
        """Select a table, a group or a relationship of the diagram (None: nothing) and show
        its panel (a match of the search is only marked: panel False)."""
        self._selected = hit
        self._sync_selection()
        if hit is None:
            self.side.pack_forget()
            return
        if panel:
            self._show_side()
        elif self.side.winfo_manager():
            self._fill_side()

    def _show_side(self):
        if not self.side.winfo_manager():
            self.side.pack(side="right", fill="y", before=self.erd)
            self.side.pack_propagate(False)
        self.side_search.set("", now=True)
        self._fill_side()

    def _side_links(self):
        hit = self._selected
        if hit is None or hit[0] == "rel":
            return []
        if hit[0] == "group":
            return [l for r in self.model.rels if r.group == hit[1] for l in r.links]
        return list(self.graph.adj.get(hit[1], ()))

    def _show_side_part(self, rel):
        """The links list (a table, a group) or the relationship's details."""
        if rel:
            self.side_list.pack_forget()
            if not self.rel_box.winfo_manager():
                self.rel_box.pack(fill="both", expand=True)
        else:
            self.rel_box.pack_forget()
            if not self.side_list.winfo_manager():
                self.side_list.pack(fill="both", expand=True)

    def _fill_side(self):
        hit = self._selected
        tree = self.side_tree
        tree.delete(*tree.get_children())
        self._side_rows = {}
        if hit is None:
            return
        bar = self.side_bar
        if hit[0] == "rel":
            self._fill_rel_side(hit[1])
            return
        self._show_side_part(False)
        if hit[0] == "group":
            g = self.model.groups.get(hit[1])
            if g is None:
                self.select(None)
                return
            listed = hit[1] in self.erd.expanded_groups()
            self.side_title.configure(text=g.label)
            self.side_info.configure(text="Linked to %s alike: %s. %s" % (
                node_label(g.anchor), "; ".join(g.lines()),
                "Listed in the diagram." if listed else "Folded into one card."))
            self.side_btns["expand"].configure(text="Fold" if listed else "List the tables")
            shown = ("browse", "focus", "expand")
        else:
            node = hit[1]
            refers, referred, across = self._counts(node)
            rows = self._rows.get(node)
            self.side_title.configure(text=node_label(node))
            self.side_info.configure(text="Refers to %d, referred by %d%s — %s rows" % (
                refers, referred, ", %d across databases" % across if across else "",
                "?" if rows is None else format(rows, ",")))
            shown = ("browse", "related", "focus")
        for k, b in self.side_btns.items():
            bar.show(b, k in shown)
        term = self.side_search.get().lower()
        me = hit[1] if hit[0] == "node" else None
        links = sorted(self._side_links(), key=lambda l: (
            l.src != me, node_label(l.dst if l.src == me else l.src).lower(),
            col_name(l.src_col)))
        n = 0
        for l in links:
            if me is None:
                way, other = "→", l.src
            elif l.direction == "peer":
                way, other = "=", (l.dst if l.src == me else l.src)
            elif l.src == me:
                way, other = "→", l.dst
            else:
                way, other = "←", l.src
            text = (way, node_label(other), l.pair_text(), l.kind_text(), l.evidence())
            if term and not any(term in str(v).lower() for v in text):
                continue
            iid = tree.insert("", "end", values=text, tags=("cross",) if l.cross else ())
            self._side_rows[iid] = (l, other)
            n += 1
        self.side_search.set_count(n, len(links), "link", "links")

    def _fill_rel_side(self, rel):
        """A relationship: its columns, kind and evidence, cardinality, a sample JOIN and
        Browse for both sides."""
        self._show_side_part(True)
        self.side_title.configure(text=rel.title())
        info = rel.kind_text()
        if rel.evidence():
            info += ", values found: %s" % rel.evidence()
        if rel.group is not None:
            g = self.model.groups.get(rel.group)
            if g is not None:
                info += " — %d tables alike (select the group for each one's link)" % len(
                    g.members)
        self.side_info.configure(text=info)
        self.rel_card.configure(text="Cardinality: " + rel.cardinality_text())
        why = rel.reason()
        self.rel_why.configure(text=("Why: " + why) if why else "")
        self.sql_text.configure(state="normal")
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", rel.join_sql(self.model))
        self.sql_text.configure(state="disabled")
        shown = ["copy_sql"]
        if isinstance(rel.src, tuple):
            shown.append("browse_src")
            self.side_btns["browse_src"].configure(text="Browse %s" % rel.src[1])
        if isinstance(rel.dst, tuple):
            shown.append("browse_dst")
            self.side_btns["browse_dst"].configure(text="Browse %s" % rel.dst[1])
        if self._rel_column(rel) is not None:
            shown.append("related")
        for k, b in self.side_btns.items():
            self.side_bar.show(b, k in shown)

    @staticmethod
    def _rel_column(rel):
        """(table node, column) the Column relationships window opens for a relationship."""
        l = rel.link
        if l is None or l.cross or rel.junction is not None or rel.group is not None:
            return None
        return l.src, l.src_col

    def sql_of_selected(self):
        return self.sql_text.get("1.0", "end").strip()

    def _side_selected(self):
        sel = self.side_tree.selection()
        return self._side_rows.get(sel[0]) if sel else None

    def _side_browse(self):
        picked = self._side_selected()
        if picked is not None:
            self.browse_node(picked[1])
        elif self._selected is not None and self._selected[0] == "node":
            self.browse_node(self._selected[1])

    def _side_action(self, key):
        hit = self._selected
        if hit is None:
            return
        if hit[0] == "rel":
            rel = hit[1]
            if key == "browse_src" and isinstance(rel.src, tuple):
                self.browse_node(rel.src)
            elif key == "browse_dst" and isinstance(rel.dst, tuple):
                self.browse_node(rel.dst)
            elif key == "copy_sql":
                self.clipboard_clear()
                self.clipboard_append(self.sql_of_selected())
            elif key == "related":
                target = self._rel_column(rel)
                if target is not None:
                    self.manager.column_map(target[0][1], target[1], self.member_of(target[0]))
            return
        picked = self._side_selected()
        if key == "browse":
            self._side_browse()
        elif key == "related":
            l = picked[0] if picked else None
            if l is None:
                ls = self._side_links()
                l = ls[0] if ls else None
            if l is not None and not l.cross and hit[0] == "node":
                col = l.src_col if l.src == hit[1] else l.dst_col
                self.manager.column_map(hit[1][1], col, self.member_of(hit[1]))
        elif key == "focus":
            target = picked[1] if picked is not None else (
                hit[1] if hit[0] == "node" else self.model.groups[hit[1]].anchor)
            self.set_focus(target)
        elif key == "expand" and hit[0] == "group":
            self.toggle_group(hit[1])
            self.select(hit)

    # -- zoom and view -------------------------------------------------------------------------
    def text_hidden(self):
        return self.erd.text_hidden()

    def zoom(self, factor, x=None, y=None):
        self.erd.zoom(factor, x, y)

    def zoom_center(self, factor):
        self.erd.zoom_center(factor)

    def fit(self, initial=False):
        self.erd.fit(initial)

    def see_key(self, key):
        self.erd.see(key)

    def see_node(self, node):
        box = self._box_of(node)
        if box is not None:
            self.erd.see(box[1])

    def highlight(self, node):
        """Select a table of the diagram (its links stand out)."""
        box = self._box_of(node)
        if box is not None:
            self.select(box)

    # -- exports -------------------------------------------------------------------------------
    def export_svg(self, path):
        """Write the diagram as it is laid out now; False when the path is refused."""
        if not write_allowed(path):
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.diagram_svg())
        return True

    def diagram_svg(self):
        """The diagram as drawn now (the cards where they are, as folded or listed)."""
        focus = None if self.overview else self.focus
        title = "Relationships: " + ("overview" if focus is None else node_label(focus))
        return self.erd.svg(title)

    def _colors(self):
        if not self._multi:
            return {}
        return dict((k, m.color) for k, m in self._members.items() if getattr(m, "color", None))

    def export_csv(self, path):
        """Write the links listed now; False when the path is refused."""
        if not write_allowed(path):
            return False
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv_writer(f)    # spreadsheet-safe text, NUL as \x00 (engine.csvcells)
            cols = self.columns()
            w.writerow([t for _c, t, _w in cols])
            for l in self.shown_links:
                w.writerow(l.row()[:len(cols)])
        return True

    def _post_export_menu(self):
        post = getattr(self.app, "_post_menu", None)
        if post is not None:
            post(self.export_menu, self.export_btn)

    def _export_members(self):
        """The databases the links listed come from (for the export's provenance)."""
        case = getattr(self.app, "case", None)
        if case is None:
            return []
        return list(case) if len(case) > 1 else [case.active]

    def _export_scope(self):
        parts = ["%d links listed" % len(self.shown_links)]
        if self.only_table_var.get() and self.focus is not None:
            parts.append("table %s" % node_label(self.focus))
        if self.search.get():
            parts.append("words: %s" % self.search.get())
        if self.weaker_var.get():
            parts.append("weaker matches included")
        if not self.empty_var.get() and self._empty_count():
            parts.append("empty tables left out")
        return "; ".join(parts)

    def export_svg_dialog(self):
        """Export ▾ › Diagram (SVG)…: the diagram as laid out now (every group listing its
        tables), written on a worker with the export's manifest (provenance, SHA-256), noted
        in the activity log."""
        path = filedialog.asksaveasfilename(defaultextension=".svg",
                                            filetypes=[("SVG", "*.svg")],
                                            initialfile="relationships.svg")
        if not path or not write_allowed(path):
            return
        from jobs import Job, manifest_text, write_export_manifest
        text = self.diagram_svg()       # read on the Tk thread
        members, scope = self._export_members(), self._export_scope()

        def work(job):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            job.status = "Writing the manifest…"
            return write_export_manifest(self.app, path, "Relationships diagram (SVG)", members,
                                         [path], True, scope=scope)

        def done(result, error, cancelled):
            if error is not None:
                messagebox.showerror("Export", "Not exported: %s" % error, parent=self)
                return
            manifest, why = result
            self.app.activity("export", what="Relationships diagram (SVG)", path=path,
                              manifest=manifest, manifest_error=why, scope=scope)
            (messagebox.showinfo if manifest else messagebox.showwarning)(
                "Export", "The diagram was written to:\n%s\n\n%s" % (
                    path, manifest_text(manifest, why)), parent=self)
        Job(self.app, "Export the diagram", work, done, members=members)

    def export_csv_dialog(self):
        """Export ▾ › Links listed (CSV)…: the links All links lists, with the one export
        writer (manifest, activity log, the same message as every export)."""
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile="relationships.csv")
        if not path or not write_allowed(path):
            return
        from jobs import export_rows
        cols = self.columns()
        names = [t for _c, t, _w in cols]
        rows = [list(l.row()[:len(cols)]) for l in self.shown_links]
        export_rows(self.app, "Export the links", path, "csv", names, lambda: iter(rows),
                    "Relationships: links listed", self._export_members(),
                    scope=self._export_scope(), total=len(rows), unit="links")


def _move_selection(tree, forward=True, want=None):
    """Select the next (or previous) line of a tree, wrapping, children of open lines
    included; want(iid) picks which lines count."""
    order = []

    def walk(parent):
        for iid in tree.get_children(parent):
            order.append(iid)
            if tree.item(iid, "open") or want is not None:
                walk(iid)
    walk("")
    if want is not None:
        order = [i for i in order if want(i)]
    if not order:
        return None
    sel = tree.selection()
    i = order.index(sel[0]) if sel and sel[0] in order else -1
    i = (i + (1 if forward else -1)) % len(order) if i >= 0 else (0 if forward else
                                                                   len(order) - 1)
    iid = order[i]
    parent = tree.parent(iid)
    while parent:
        tree.item(parent, open=True)
        parent = tree.parent(parent)
    tree.selection_set(iid)
    tree.see(iid)
    return iid
