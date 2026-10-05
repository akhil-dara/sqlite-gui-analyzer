"""The breadcrumb bar of the tabs that work on one database: '● accounts_db ▾ › account ▾'.

The database part says which database the tab shows (its colour dot and name); in a case of
several it is a searchable dropdown that makes another database the active one. Browse adds the
table part (its table dropdown). A tab always says where its rows come from.
"""

import tkinter as tk
from tkinter import ttk

from combobox import SearchableCombobox
from tokens import COLOR as K, FONT as F, XS, S
from widgets import ToolTip


class Breadcrumb(ttk.Frame):
    """app gives case, activate_member; table_var: a StringVar for the table part (Browse);
    then .table_combo is the table dropdown."""

    def __init__(self, master, app, table_var=None, table_width=28):
        ttk.Frame.__init__(self, master)
        self.app = app
        self.dot = tk.Canvas(self, width=12, height=12, highlightthickness=0,
                             background=K["background"])
        self.dot.pack(side="left", padx=(0, XS))
        self.name = ttk.Label(self, text="No database", style="Heading.TLabel")
        self.var = tk.StringVar(master=self)
        self.db_combo = SearchableCombobox(self, textvariable=self.var, state="readonly",
                                           width=24, postcommand=self._fill,
                                           cycle_on_arrows=True)
        self.db_combo.bind("<<ComboboxSelected>>", lambda e: self._chosen())
        ToolTip(self.db_combo, "The database this tab shows: choose another one of the case "
                               "to make it the active database")
        self.sep = ttk.Label(self, text="›", style="Muted.TLabel", font=F["label"])
        self.table_combo = None
        if table_var is not None:
            self.table_combo = SearchableCombobox(self, textvariable=table_var,
                                                  state="readonly", width=table_width,
                                                  placeholder="Choose a table",
                                                  cycle_on_arrows=True)
        self._multi = None
        self.refresh()

    def _members(self):
        return list(getattr(self.app, "case", []) or [])

    def _fill(self):
        from navigator import group_key
        ms = self._members()
        by = {}
        for m in ms:
            by.setdefault(group_key(m.path), []).append(m.name)
        if len(by) > 1:
            self.db_combo.configure(groups=sorted(by.items(), key=lambda kv: kv[0].lower()))
        else:
            self.db_combo.configure(values=[m.name for m in ms])

    def _chosen(self):
        name = self.var.get()
        for m in self._members():
            if m.name == name:
                self.app.activate_member(m)
                return

    def refresh(self):
        """Show the active database (and, in a case, the dropdown to switch)."""
        case = getattr(self.app, "case", None)
        active = getattr(case, "active", None) if case is not None else None
        multi = len(self._members()) > 1
        self.dot.delete("all")
        if active is not None:
            self.dot.create_oval(1, 1, 11, 11, fill=active.color if multi else K["primary"],
                                 outline="")
        text = active.name if active is not None else "No database open"
        if multi != self._multi:
            self._multi = multi
            for w in (self.name, self.db_combo, self.sep):
                w.pack_forget()
            if self.table_combo is not None:
                self.table_combo.pack_forget()
            (self.db_combo if multi else self.name).pack(side="left")
            if self.table_combo is not None:
                self.sep.pack(side="left", padx=S)
                self.table_combo.pack(side="left")
        if multi:
            self._fill()
            self.var.set(text)
        else:
            self.name.configure(text=text)

    def text(self):
        """The breadcrumb as it reads ('msgstore.db › message')."""
        case = getattr(self.app, "case", None)
        active = getattr(case, "active", None) if case is not None else None
        parts = [active.name if active is not None else "No database open"]
        if self.table_combo is not None and self.table_combo.get():
            parts.append(self.table_combo.get())
        return " › ".join(parts)
