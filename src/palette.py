"""The command palette (Ctrl+K): one search box over everything that can be opened or done.

Items: the databases, tables and columns of every database of the case, the tabs, and the
actions ('Build timeline', 'Export Database Map…', 'Find value…'). Typing ranks them (exact,
prefix, word start, substring, then letters in order: 'msgdb' finds msgstore.db); Enter opens
the highlighted one; recent choices come first while nothing is typed. score() and rank() are
plain functions (no Tk).
"""

import tkinter as tk
from tkinter import ttk

from engine import limits
from tokens import COLOR as K, FONT as F, XS, S, M
from widgets import place_over

KIND_NAMES = {"database": "Database", "table": "Table", "column": "Column", "tab": "Tab",
              "action": "Action"}
KIND_ORDER = {"action": 0, "tab": 1, "database": 2, "table": 3, "column": 4}


def score(query, text):
    """A rank (lower is better) of how text matches query, or None: 0 exact, 1 prefix,
    2 a word starts with it, 3 substring, 4 every word found, 5 letters in order."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 6
    if t == q:
        return 0
    if t.startswith(q):
        return 1
    i = t.find(q)
    if i > 0 and not t[i - 1].isalnum():
        return 2
    if i >= 0:
        return 3
    words = q.split()
    if len(words) > 1 and all(w in t for w in words):
        return 4
    pos = 0
    for ch in q.replace(" ", ""):
        pos = t.find(ch, pos)
        if pos < 0:
            return None
        pos += 1
    return 5


class Item(object):
    __slots__ = ("kind", "label", "where", "key", "run", "text")

    def __init__(self, kind, label, where, key, run):
        self.kind, self.label, self.where, self.key, self.run = kind, label, where, key, run
        self.text = label if kind in ("action", "tab") else "%s %s" % (label, where)


def rank(query, items, recent=()):
    """[(item, score)] of the items matching query, best first (recent ones first while the
    query is empty)."""
    if not query.strip():
        order = dict((k, i) for i, k in enumerate(recent))
        rec = sorted([it for it in items if it.key in order], key=lambda it: order[it.key])
        rest = [it for it in items if it.key not in order and it.kind in ("action", "tab")]
        return [(it, -1) for it in rec] + [(it, 6) for it in rest]
    out = []
    for i, it in enumerate(items):
        s = score(query, it.label)
        if s is None and it.where:
            s2 = score(query, it.text)
            s = None if s2 is None else s2 + 1
        if s is not None:
            out.append((s, KIND_ORDER.get(it.kind, 9), i, it))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(it, s) for s, _k, _i, it in out]


class CommandPalette(tk.Toplevel):
    """The palette window: items() gives the list (built when opened)."""

    def __init__(self, app, items, recent=(), on_chosen=None):
        tk.Toplevel.__init__(self, app)
        self.app, self.items, self.recent = app, list(items), list(recent)
        self.on_chosen = on_chosen
        self.title("Go to…")
        self.transient(app)
        self.configure(background=K["border"])
        self.resizable(False, False)
        body = ttk.Frame(self, style="Popover.TFrame", padding=(M, M, M, S))
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.var = tk.StringVar(master=self)
        self.entry = ttk.Entry(body, textvariable=self.var, style="Search.TEntry", width=60)
        self.entry.pack(fill="x")
        from widgets import placeholder_label
        self._ph = placeholder_label(self.entry, "Find a database, table, column, tab or "
                                                 "action…", F["large"])
        self._ph.place(x=8, rely=0.5, anchor="w", relwidth=1.0, width=-16)
        self.tree = ttk.Treeview(body, columns=("kind", "where"), show="tree",
                                 selectmode="browse", height=14)
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("kind", width=80, stretch=False)
        self.tree.column("where", width=220, stretch=False)
        self.tree.pack(fill="both", expand=True, pady=(S, XS))
        self.tree.tag_configure("recent", foreground=K["heading"])
        self.footer = ttk.Label(body, text="", style="CardMuted.TLabel")
        self.footer.pack(fill="x")
        self.var.trace_add("write", lambda *a: self.fill())
        for w in (self.entry, self.tree):
            w.bind("<Down>", lambda e: self.move(1))
            w.bind("<Up>", lambda e: self.move(-1))
            w.bind("<Next>", lambda e: self.move(10))
            w.bind("<Prior>", lambda e: self.move(-10))
            w.bind("<Return>", lambda e: self.choose())
            w.bind("<Escape>", lambda e: self.close())
        self.tree.bind("<Double-1>", lambda e: self.choose())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._shown = []
        self.fill()
        place_over(self, app)
        try:
            self.entry.focus_force()
        except tk.TclError:
            pass

    def fill(self):
        q = self.var.get()
        if q:
            self._ph.place_forget()
        else:
            self._ph.place(x=8, rely=0.5, anchor="w", relwidth=1.0, width=-16)
        ranked = rank(q, self.items, self.recent)
        most = limits.get("palette_results")
        shown = ranked[:most]
        tree = self.tree
        tree.delete(*tree.get_children())
        self._shown = []
        for i, (it, s) in enumerate(shown):
            iid = tree.insert("", "end", text=it.label, values=(KIND_NAMES.get(it.kind, it.kind),
                                                                it.where),
                              tags=("recent",) if s == -1 else ())
            self._shown.append((iid, it))
        if self._shown:
            tree.selection_set(self._shown[0][0])
            tree.focus(self._shown[0][0])
        if not q:
            self.footer.configure(text="Recent first · Enter opens · Esc closes · "
                                       "type to find any database, table or column")
        elif not ranked:
            self.footer.configure(text="Nothing matches “%s”" % q)
        elif len(ranked) > len(shown):
            self.footer.configure(text="%s of %s matches listed (limit palette_results): "
                                       "type more to narrow" % (format(len(shown), ","),
                                                                format(len(ranked), ",")))
        else:
            self.footer.configure(text="%s match%s" % (format(len(ranked), ","),
                                                       "" if len(ranked) == 1 else "es"))

    def shown(self):
        return [it for _iid, it in self._shown]

    def move(self, n):
        if not self._shown:
            return "break"
        cur = self.tree.selection()
        ids = [iid for iid, _it in self._shown]
        i = ids.index(cur[0]) if cur and cur[0] in ids else 0
        i = max(0, min(len(ids) - 1, i + n))
        self.tree.selection_set(ids[i])
        self.tree.focus(ids[i])
        self.tree.see(ids[i])
        return "break"

    def choose(self):
        sel = self.tree.selection()
        item = next((it for iid, it in self._shown if sel and iid == sel[0]), None)
        self.close()
        if item is not None:
            if self.on_chosen is not None:
                self.on_chosen(item)
            item.run()
        return "break"

    def close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass
        return "break"
