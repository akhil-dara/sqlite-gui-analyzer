"""Which databases of a case each feature covers: one scope control everywhere.

Scopes        the model (no Tk). One global scope per case (all databases, or a chosen set),
              remembered in the case file; each feature (search, timeline, relations, find,
              datamap) follows it, or has its own choice until 'Follow the global scope' is
              clicked. Saved scopes ('Messaging DBs') keep a named set of databases.
scope_text    the one short line a scope is said in: 'All 16 databases', '3 of 16 databases'.
ScopePicker   the button ('All 16 databases ▾') and its popover: a search box, the databases
              grouped by app or folder with tri-state checks and counts, presets (All, Only
              active, Selected in navigator, Databases with hits, With dates), saved scopes,
              'Only for this tab' / 'Follow the global scope'. Changes apply when the popover
              closes (Done, Escape keeps what was ticked; Cancel drops it).
"""

import tkinter as tk
from tkinter import ttk

from tokens import COLOR as K, FONT as F, XS, S, M
from widgets import SearchBox, ToolTip, menu_button

FEATURES = ("search", "timeline", "relations", "find", "datamap")
FEATURE_NAMES = {"search": "Search", "timeline": "Timeline", "relations": "Relationships",
                 "find": "Find everywhere", "datamap": "Database Map"}
CHECK, UNCHECK, PARTIAL = "☑", "☐", "◪"


def scope_text(n, total, noun="database"):
    """'All 16 databases', '3 of 16 databases', 'msgstore.db' is given by the caller."""
    nouns = noun + "s"
    if total <= 1:
        return "1 %s" % noun if total == 1 else "No %s" % nouns
    if n >= total:
        return "All %d %s" % (total, nouns)
    return "%d of %d %s" % (n, total, nouns if total != 1 else noun)


class Scopes(object):
    """The scope of every feature of the open case. members() gives the case members (a
    callable, so the model follows the case as databases join and leave)."""

    def __init__(self, members):
        self._members = members
        self.global_sel = None          # None: every database; else a set of uids
        self.own = {}                   # feature -> set of uids (its own choice)
        self.saved = {}                 # name -> [paths]
        self.listeners = []             # fn(features changed: list)

    # -- reading ----------------------------------------------------------------------------
    def all(self):
        return list(self._members())

    def follows(self, feature):
        return feature not in self.own

    def selection(self, feature=None):
        """None (every database) or the set of uids the feature covers."""
        sel = self.own.get(feature) if feature is not None and feature in self.own \
            else self.global_sel
        if sel is None:
            return None
        known = set(m.uid for m in self.all())
        sel = set(u for u in sel if u in known)
        if not sel or sel == known:
            return None
        return sel

    def members(self, feature=None):
        sel = self.selection(feature)
        return [m for m in self.all() if sel is None or m.uid in sel]

    def text(self, feature=None):
        ms, total = self.members(feature), len(self.all())
        if total > 1 and len(ms) == 1:
            return ms[0].name
        return scope_text(len(ms), total)

    # -- changing ---------------------------------------------------------------------------
    def _clean(self, uids):
        known = set(m.uid for m in self.all())
        sel = None if uids is None else set(u for u in uids if u in known)
        if sel is not None and (not sel or sel == known):
            sel = None
        return sel

    def set(self, feature, uids, own=None):
        """Set a feature's scope: its own choice when own (or when it has one already), else
        the global scope (which every following feature takes)."""
        if own or (own is None and feature in self.own):
            sel = self._clean(uids)
            new = sel if sel is not None else set(m.uid for m in self.all())
            if self.own.get(feature) != new:
                self.own[feature] = new
                self._notify([feature])
        else:
            was_own = self.own.pop(feature, None) is not None
            if not self.set_global(uids) and was_own:
                self._notify([feature])

    def set_global(self, uids):
        """Returns True when the global scope changed (the following features are told)."""
        sel = self._clean(uids)
        if sel == self.global_sel:
            return False
        self.global_sel = sel
        self._notify([f for f in FEATURES if f not in self.own])
        return True

    def follow(self, feature):
        if feature in self.own:
            del self.own[feature]
            self._notify([feature])

    def detach(self, feature):
        """Give a feature its own choice (starting from what it covers now)."""
        if feature not in self.own:
            sel = self.selection(feature)
            self.own[feature] = sel if sel is not None else set(m.uid for m in self.all())
            self._notify([feature])

    def forget(self, uid):
        """A database left the case."""
        if self.global_sel is not None:
            self.global_sel.discard(uid)
            if not self.global_sel:
                self.global_sel = None
        for f in list(self.own):
            self.own[f].discard(uid)
            if not self.own[f]:
                del self.own[f]

    def save_as(self, name, uids):
        paths = [m.path for m in self.all() if m.uid in set(uids)]
        if name and paths:
            self.saved[name] = paths

    def saved_uids(self, name):
        import os
        want = set(os.path.normcase(os.path.abspath(p)) for p in self.saved.get(name, ()))
        return [m.uid for m in self.all() if os.path.normcase(os.path.abspath(m.path)) in want]

    def _notify(self, features):
        for fn in list(self.listeners):
            fn(features)

    # -- the case file ------------------------------------------------------------------------
    def to_state(self):
        by_uid = dict((m.uid, m.path) for m in self.all())

        def paths(sel):
            return None if sel is None else sorted(by_uid[u] for u in sel if u in by_uid)
        return {"global": paths(self.global_sel),
                "own": dict((f, paths(s)) for f, s in self.own.items()),
                "saved": dict(self.saved)}

    def load_state(self, state):
        """Restore what to_state() gave (paths no longer in the case are dropped)."""
        import os
        if not isinstance(state, dict):
            return
        norm = dict((os.path.normcase(os.path.abspath(m.path)), m.uid) for m in self.all())

        def uids(paths):
            if not isinstance(paths, list):
                return None
            return set(norm[p] for p in (os.path.normcase(os.path.abspath(x)) for x in paths
                                          if isinstance(x, str)) if p in norm)
        self.global_sel = self._clean(uids(state.get("global")))
        self.own = {}
        for f, paths in (state.get("own") or {}).items():
            if f in FEATURES:
                sel = uids(paths)
                if sel:
                    self.own[f] = sel
        saved = state.get("saved")
        if isinstance(saved, dict):
            self.saved = dict((str(k), [p for p in v if isinstance(p, str)])
                              for k, v in saved.items() if isinstance(v, list))
        self._notify(list(FEATURES))


class ScopePicker(ttk.Frame):
    """The scope button of one feature and its popover. app gives case, scopes, the
    navigator's selection, search hits and dates; on_change() after the scope changed."""

    def __init__(self, master, app, feature, on_change=None, prefix="", own_changes_only=False):
        ttk.Frame.__init__(self, master)
        self.app, self.feature, self.on_change = app, feature, on_change
        self.prefix = prefix
        # True: on_change only for a choice made in this picker (a window showing results
        # does not start again because the scope was changed somewhere else)
        self.own_changes_only = own_changes_only
        self._applying = False
        self.button = ttk.Button(self, text="", style="TButton", command=self.open)
        self.button.pack(side="left")
        self.follow_lbl = ttk.Label(self, text="", style="Link.TLabel", cursor="hand2")
        self.follow_lbl.bind("<Button-1>", lambda e: self.follow())
        self._pop = None
        self._tip = ToolTip(self.button, "")
        app.scopes.listeners.append(self._changed)
        self.refresh()

    def destroy(self):
        try:
            self.app.scopes.listeners.remove(self._changed)
        except (ValueError, AttributeError):
            pass
        self.close(apply=False)
        ttk.Frame.destroy(self)

    # -- the ttk.Menubutton-like API older code and tests use -------------------------------
    def selection(self):
        return self.app.scopes.selection(self.feature)

    def members(self):
        return self.app.scopes.members(self.feature)

    def set_selection(self, uids):
        self.app.scopes.set(self.feature, uids)

    def rebuild(self):
        self.refresh()

    def cget(self, key):
        if key == "text":
            return self.button.cget("text")
        return ttk.Frame.cget(self, key)

    # -- display ----------------------------------------------------------------------------
    def _changed(self, features):
        if self.feature in features:
            self.refresh()
            if self.on_change is not None and (self._applying or not self.own_changes_only):
                self.on_change()

    def refresh(self):
        scopes = self.app.scopes
        text = scopes.text(self.feature)
        own = not scopes.follows(self.feature)
        self.button.configure(text="%s%s ▾" % (self.prefix, text))
        names = [m.name for m in scopes.members(self.feature)]
        self._tip.text = "%s covers: %s%s\n%s" % (
            FEATURE_NAMES.get(self.feature, self.feature),
            ", ".join(names[:12]) + (" and %d more" % (len(names) - 12) if len(names) > 12
                                     else ""),
            "" if not own else "\n(its own choice, not the global scope)",
            "Click to choose the databases.")
        if own and len(scopes.all()) > 1:
            self.follow_lbl.configure(text="Follow global scope")
            if not self.follow_lbl.winfo_manager():
                self.follow_lbl.pack(side="left", padx=(S, 0))
        elif self.follow_lbl.winfo_manager():
            self.follow_lbl.pack_forget()

    def follow(self):
        self.app.scopes.follow(self.feature)

    # -- the popover ------------------------------------------------------------------------
    def is_open(self):
        return self._pop is not None

    def open(self):
        if self._pop is not None:
            self.close(apply=True)
            return
        if len(self.app.scopes.all()) <= 1:
            return
        self._pop = ScopePopover(self, self.app, self.feature)

    def close(self, apply=True):
        pop, self._pop = self._pop, None
        if pop is None:
            return
        if apply:
            self._applying = True
            try:
                pop.apply()
            finally:
                self._applying = False
        try:
            pop.destroy()
        except tk.TclError:
            pass


class ScopePopover(tk.Toplevel):
    """The databases of the case with tri-state group checks, a search box, presets and
    saved scopes. ticked: the uids chosen so far (applied by apply())."""

    def __init__(self, picker, app, feature):
        tk.Toplevel.__init__(self, picker)
        self.picker, self.app, self.feature = picker, app, feature
        self.wm_overrideredirect(True)
        self.configure(background=K["border"])
        scopes = app.scopes
        sel = scopes.selection(feature)
        self.ticked = set(m.uid for m in scopes.all()) if sel is None else set(sel)
        self.own_var = tk.BooleanVar(master=self, value=not scopes.follows(feature))
        body = ttk.Frame(self, style="Popover.TFrame", padding=(M, S, M, S))
        body.pack(fill="both", expand=True, padx=1, pady=1)
        top = ttk.Frame(body, style="Plain.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Databases %s covers" % FEATURE_NAMES.get(feature, feature),
                  style="CardHeading.TLabel").pack(side="left")
        self.count = ttk.Label(top, text="", style="CardMuted.TLabel")
        self.count.pack(side="right")
        presets = ttk.Frame(body, style="Plain.TFrame")
        presets.pack(fill="x", pady=(S, XS))
        self._presets = {}
        for key, text in (("all", "All"), ("active", "Only active"),
                          ("nav", "Selected in navigator"), ("hits", "With hits"),
                          ("dates", "With dates")):
            b = ttk.Button(presets, text=text, style="Small.TButton",
                           command=lambda k=key: self.preset(k))
            b.pack(side="left", padx=(0, XS))
            self._presets[key] = b
        self.search = SearchBox(body, placeholder="Find a database…", delay=0, width=28,
                                find_button=False, on_change=lambda t: self.fill())
        self.search.pack(fill="x", pady=(XS, XS))
        box = ttk.Frame(body, style="Plain.TFrame")
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("info",), show="tree", selectmode="browse",
                                 height=12)
        self.tree.column("#0", width=250, stretch=True)
        self.tree.column("info", width=120, anchor="e", stretch=False)
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("group", font=F["small_bold"], foreground=K["muted_text"])
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda e: self._toggle_focus())
        self.tree.bind("<Return>", lambda e: self.done())
        opts = ttk.Frame(body, style="Plain.TFrame")
        opts.pack(fill="x", pady=(S, 0))
        self.own_cb = ttk.Checkbutton(opts, text="Only for %s (not the global scope)"
                                      % FEATURE_NAMES.get(feature, feature),
                                      variable=self.own_var, style="Card.TCheckbutton")
        self.own_cb.pack(side="left")
        saved = ttk.Frame(body, style="Plain.TFrame")
        saved.pack(fill="x", pady=(XS, 0))
        self.saved_btn, self.saved_menu = menu_button(saved, "Saved scopes ▾",
                                                    postcommand=self._fill_saved)
        self.saved_btn.pack(side="left")
        btns = ttk.Frame(body, style="Plain.TFrame")
        btns.pack(fill="x", pady=(S, 0))
        ttk.Button(btns, text="Done", style="Primary.TButton", command=self.done).pack(
            side="right")
        ttk.Button(btns, text="Cancel", command=self.cancel).pack(side="right", padx=XS)
        self.bind("<Escape>", lambda e: self.done())
        self._items = {}
        self.fill()
        self._place()
        try:
            self.search.entry.focus_force()
        except tk.TclError:
            pass

    def _place(self):
        try:
            self.update_idletasks()
            b = self.picker.button
            x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + 2
            self.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def groups(self):
        from navigator import group_key
        by = {}
        for m in self.app.scopes.all():
            by.setdefault(group_key(m.path), []).append(m)
        return sorted(by.items(), key=lambda kv: kv[0].lower())

    def fill(self):
        tree = self.tree
        tree.delete(*tree.get_children())
        self._items = {}
        q = self.search.get().lower()
        groups = self.groups()
        hits = getattr(getattr(self.app, "_navigator", None), "hits", {}) or {}
        for gname, ms in groups:
            shown = [m for m in ms if not q or q in m.name.lower() or q in gname.lower()]
            if not shown:
                continue
            parent = ""
            if len(groups) > 1:
                n_on = sum(1 for m in ms if m.uid in self.ticked)
                mark = CHECK if n_on == len(ms) else UNCHECK if not n_on else PARTIAL
                parent = tree.insert("", "end", text="%s  %s" % (mark, gname), open=True,
                                     values=("%d of %d" % (n_on, len(ms)),), tags=("group",))
                self._items[parent] = ("group", ms)
            for m in shown:
                from navigator import member_rows, short_count
                rows = member_rows(m)[0]
                info = "%d tables · %s rows" % (len(m.db.tables()), short_count(rows))
                if hits.get(m.uid):
                    info = "%s hits · " % short_count(hits[m.uid]) + info
                iid = tree.insert(parent, "end", text="%s  %s" % (
                    CHECK if m.uid in self.ticked else UNCHECK, m.name), values=(info,))
                self._items[iid] = ("db", m)
        total = len(self.app.scopes.all())
        self.count.configure(text=scope_text(len(self.ticked), total))
        nav = getattr(self.app, "_navigator", None)
        self._presets["nav"].configure(state="normal" if nav is not None and len(
            nav.selected_members()) >= 1 else "disabled")
        self._presets["hits"].configure(state="normal" if any(hits.values()) else "disabled")
        dates = getattr(nav, "dates", {}) if nav is not None else {}
        self._presets["dates"].configure(state="normal" if any(dates.values()) else
                                         "disabled")

    def _click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.toggle(iid)
            return "break"
        return None

    def _toggle_focus(self):
        iid = self.tree.focus()
        if iid:
            self.toggle(iid)
        return "break"

    def toggle(self, iid):
        kind, x = self._items.get(iid, (None, None))
        if kind == "db":
            if x.uid in self.ticked:
                if len(self.ticked) > 1:        # at least one database stays
                    self.ticked.discard(x.uid)
            else:
                self.ticked.add(x.uid)
        elif kind == "group":
            uids = set(m.uid for m in x)
            if uids <= self.ticked:
                rest = self.ticked - uids
                if rest:
                    self.ticked = rest
            else:
                self.ticked |= uids
        self.fill()

    def preset(self, key):
        allm = self.app.scopes.all()
        nav = getattr(self.app, "_navigator", None)
        if key == "all":
            uids = [m.uid for m in allm]
        elif key == "active":
            a = getattr(self.app.case, "active", None)
            uids = [a.uid] if a is not None else []
        elif key == "nav":
            uids = nav.selected_members(uids=True) if nav is not None else []
        elif key == "hits":
            uids = [u for u, n in (getattr(nav, "hits", {}) or {}).items() if n]
        else:
            uids = [u for u, n in (getattr(nav, "dates", {}) or {}).items() if n]
        if uids:
            self.ticked = set(uids)
            self.fill()

    def _fill_saved(self):
        m = self.saved_menu
        m.delete(0, "end")
        scopes = self.app.scopes
        for name in sorted(scopes.saved):
            m.add_command(label="%s (%d)" % (name, len(scopes.saved_uids(name))),
                          command=lambda n=name: self._use_saved(n))
        if scopes.saved:
            m.add_separator()
        m.add_command(label="Save the ticked databases as…", command=self._save_as)

    def _use_saved(self, name):
        uids = self.app.scopes.saved_uids(name)
        if uids:
            self.ticked = set(uids)
            self.fill()

    def _save_as(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("Save scope", "Name of this set of %d databases:"
                                      % len(self.ticked), parent=self)
        if name and name.strip():
            self.app.scopes.save_as(name.strip(), self.ticked)
            saver = getattr(self.app, "_save_case", None)
            if saver is not None:
                saver()

    def apply(self):
        self.app.scopes.set(self.feature, self.ticked, own=bool(self.own_var.get()))

    def done(self):
        self.picker.close(apply=True)

    def cancel(self):
        self.picker.close(apply=False)
