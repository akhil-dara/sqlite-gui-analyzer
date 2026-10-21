"""'Missing row numbers…' of a table (engine.forensics.missing): which rowids the table no
longer has - deleted rows, recovered or not - as a sentence, a strip from 1 to the highest
number with the missing ranges marked (recovered ones in their own colour), and the ranges in
a list, largest first or in order. The window reads through the session (read-only) on its own
worker; Export CSV writes the ranges to a file the user picks (never next to the evidence).
"""

import csv
import tkinter as tk
from tkinter import filedialog, ttk

from constants import C
from grid import Runner
from tokens import COLOR as K, FONT as F
from widgets import FlowFrame, wrap_to_width

MAX_LISTED = 5000               # ranges listed (all are counted and drawn)


class MissingRowsWindow(tk.Toplevel):
    def __init__(self, app, member, table):
        tk.Toplevel.__init__(self, app)
        self.app, self.member, self.table = app, member, table
        self.title("Missing row numbers — %s" % table)
        self.configure(bg=C["bg"])
        self.transient(app)
        self.geometry("760x560")
        self.result = None
        self.runner = Runner(self, "missing")
        self.head = wrap_to_width(ttk.Label(self, text="Reading the row numbers of %s…" % table,
                                            style="Heading.TLabel"))
        self.head.pack(fill="x", padx=14, pady=(12, 2))
        self.caveat = wrap_to_width(ttk.Label(
            self, style="M.TLabel", text="A missing number is usually a deleted row. An app "
            "can also choose its numbers itself and skip some, and changing a row's number "
            "moves it: check the rows around a gap."))
        self.caveat.pack(fill="x", padx=14)
        self.strip = tk.Canvas(self, height=46, bg=K["card"], highlightthickness=0)
        self.strip.pack(fill="x", padx=14, pady=(10, 0))
        self.strip.bind("<Configure>", lambda e: self._draw())
        self.legend = ttk.Label(self, style="M.TLabel", text="")
        self.legend.pack(anchor="w", padx=14)
        top = FlowFrame(self)
        top.pack(fill="x", padx=14, pady=(8, 2))
        top.add(ttk.Label(top, text="Ranges of missing numbers"))
        self.order = tk.StringVar(value="size")
        top.add(ttk.Radiobutton(top, text="largest first", value="size", variable=self.order,
                                command=self._fill), gap=10)
        top.add(ttk.Radiobutton(top, text="in order", value="order", variable=self.order,
                                command=self._fill), gap=6)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=14)
        self.tree = ttk.Treeview(box, columns=("from", "to", "n", "where", "rec"),
                                 show="headings", selectmode="browse")
        for c, t, w, anchor in (("from", "From", 110, "e"), ("to", "To", 110, "e"),
                                ("n", "How many", 90, "e"), ("where", "Where", 210, "w"),
                                ("rec", "Recovered", 120, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor, stretch=c == "where")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.note = ttk.Label(self, style="M.TLabel", text="")
        self.note.pack(anchor="w", padx=14)
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=14, pady=10)
        bar.add(ttk.Button(bar, text="Browse the table", command=self._browse))
        self.export_btn = bar.add(ttk.Button(bar, text="Export the ranges (CSV)…",
                                             command=self.export), gap=6)
        bar.add(ttk.Button(bar, text="Close", command=self.destroy), gap=12)
        self.bind("<Escape>", lambda e: self.destroy())
        self._start()

    def _records(self):
        fx = getattr(self.app, "_forensics", None)
        if fx is None or not getattr(fx, "_scanned", False) or \
                getattr(fx, "db", None) is not self.member.db:
            return ()
        return list(getattr(fx, "_records", None) or ())

    def _start(self):
        session = self.member.db.session
        records = self._records()
        self.scanned = bool(records) or getattr(getattr(self.app, "_forensics", None),
                                                "_scanned", False)

        def work():
            return session.forensics.missing_keys(self.table, records)

        def done(m, error):
            if not self.winfo_exists():
                return
            if error is not None:
                self.head.configure(text="The row numbers could not be read: %s" % error)
                return
            self.result = m
            self.head.configure(text=m.text())
            self._draw()
            self._fill()
        self.runner.submit("missing", work, done)

    def _where(self, a, b):
        m = self.result
        if m.hi is not None and a > m.hi:
            return "after the last live row"
        if m.lo is not None and b < m.lo:
            return "before the first live row"
        return "between live rows %s and %s" % (format(a - 1, ","), format(b + 1, ","))

    def _recovered_in(self, a, b):
        rec = self.result.recovered
        if not rec:
            return 0
        if b - a > 100000:
            return sum(1 for k in rec if a <= k <= b)
        return sum(1 for k in range(a, b + 1) if k in rec)

    def _fill(self):
        t = self.tree
        t.delete(*t.get_children())
        m = self.result
        if m is None or m.reason:
            return
        ranges = list(m.ranges)
        if self.order.get() == "size":
            ranges.sort(key=lambda r: (-(r[1] - r[0]), r[0]))
        for i, (a, b) in enumerate(ranges[:MAX_LISTED]):
            n = self._recovered_in(a, b)
            t.insert("", "end", iid=str(i), values=(
                format(a, ","), format(b, ","), format(b - a + 1, ","), self._where(a, b),
                ("%s of %s" % (format(n, ","), format(b - a + 1, ","))) if n else
                ("none" if self.scanned else "run Recover to see")))
        self.note.configure(text="%s ranges%s." % (
            format(len(ranges), ","), " - the first %s listed (all drawn above and counted)"
            % format(MAX_LISTED, ",") if len(ranges) > MAX_LISTED else ""))

    def _draw(self):
        c = self.strip
        c.delete("all")
        m = self.result
        if m is None or m.reason or not m.top():
            return
        w = max(100, c.winfo_width())
        top = m.top()
        x0, x1, y0, y1 = 4, w - 4, 6, 30

        def x(k):
            return x0 + (x1 - x0) * (k - 1) / max(1, top)
        c.create_rectangle(x0, y0, x1, y1, fill=K["secondary"], outline="")
        for a, b in m.ranges:
            c.create_rectangle(x(a), y0, max(x(a) + 1, x(b + 1)), y1, fill=K["accent"],
                               outline="")
        for k in m.recovered:
            c.create_rectangle(x(k), y0, x(k) + 1, y1, fill=K["success"], outline="")
        c.create_text(x0, y1 + 3, anchor="nw", text="1", font=F["tiny"], fill=K["muted_text"])
        c.create_text(x1, y1 + 3, anchor="ne", text=format(top, ","), font=F["tiny"],
                      fill=K["muted_text"])
        self.legend.configure(text="blue: live rows   amber: missing numbers   green: missing "
                                   "but recovered (Forensics)")

    def _browse(self):
        self.app.browse_member_table(self.member, self.table)

    def export(self, path=None):
        from utils import write_allowed
        m = self.result
        if m is None or m.reason:
            return None
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self, title="Export the missing row numbers", defaultextension=".csv",
                initialfile="%s_missing_rows.csv" % self.table,
                filetypes=[("CSV", "*.csv")])
        if not path or not write_allowed(path):
            return None
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["# %s" % m.text()])
            w.writerow(["# database", self.member.path])
            w.writerow(["from", "to", "how_many", "where", "recovered"])
            for a, b in m.ranges:
                w.writerow([a, b, b - a + 1, self._where(a, b), self._recovered_in(a, b)])
        return path
