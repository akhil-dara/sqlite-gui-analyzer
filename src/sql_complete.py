"""Completion for the SQL editor: while typing, a list under the cursor offers the tables,
the columns (with their declared types; columns of the tables the statement already names
first, and after 'alias.' only that table's columns), keywords and SQLite functions.

Keys: Up / Down move, Tab or Enter take the choice, Escape closes, Ctrl+Space opens the list
at any point. A click takes a choice; clicking elsewhere or leaving the editor closes it.
Identifiers that need quoting are inserted quoted. Nothing is ever run: it only types.
"""

import re
import tkinter as tk

from tokens import COLOR as K, FONT as F

KEYWORDS = (
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "GLOB",
    "BETWEEN", "JOIN", "LEFT JOIN", "INNER JOIN", "CROSS JOIN", "ON", "AS", "ORDER BY",
    "GROUP BY", "ASC", "DESC", "HAVING", "LIMIT", "OFFSET", "DISTINCT", "UNION", "UNION ALL",
    "EXCEPT", "INTERSECT", "WITH", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "EXISTS",
    "ESCAPE", "COLLATE", "NOCASE", "ROWID")
FUNCTIONS = (
    "count", "sum", "avg", "min", "max", "total", "group_concat", "coalesce", "ifnull",
    "nullif", "iif", "substr", "length", "trim", "ltrim", "rtrim", "upper", "lower", "replace",
    "instr", "printf", "format", "hex", "unhex", "quote", "typeof", "abs", "round", "random",
    "date", "time", "datetime", "julianday", "unixepoch", "strftime", "json_extract",
    "json_each", "json_tree", "json_array_length", "json_type", "json_valid", "char",
    "unicode", "zeroblob", "likely", "glob", "like")
MAX_ITEMS = 60
RESERVED = frozenset(w for k in KEYWORDS for w in k.split()) | frozenset((
    "ORDER", "GROUP", "TABLE", "INDEX", "KEY", "PRIMARY", "REFERENCES", "VALUES", "DEFAULT",
    "CHECK", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRANSACTION",
    "TRIGGER", "VIEW", "UNIQUE", "CONSTRAINT", "FOREIGN", "INTO", "SET", "TO", "BY", "IF",
    "NATURAL", "USING", "OUTER", "FULL", "RIGHT", "REPLACE", "ABORT", "COMMIT", "ROLLBACK"))
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_TAIL = re.compile(r"[A-Za-z0-9_$]*\Z")
_OWNER = re.compile(r'("(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)\Z')
_SOURCES = re.compile(r'\b(?:FROM|JOIN)\s+("(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
                      r'(?:\s+(?:AS\s+)?(?!ON\b|WHERE\b|JOIN\b|LEFT\b|INNER\b|CROSS\b|'
                      r'GROUP\b|ORDER\b|LIMIT\b|NATURAL\b|USING\b)'
                      r'("(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*))?', re.I)


def quote(name):
    """name as typed in SQL: bare when it is a plain identifier and no keyword."""
    if _IDENT.match(name) and name.upper() not in RESERVED:
        return name
    return '"%s"' % name.replace('"', '""')


def _unquote(name):
    if name.startswith('"'):
        return name.strip('"').replace('""', '"')
    return name


def word_at(before):
    """(owner, word) for the text before the cursor: the identifier being typed, and the
    alias or table before a '.' right in front of it (else None)."""
    word = _TAIL.search(before).group(0)
    rest = before[:len(before) - len(word)]
    owner = None
    if rest.endswith("."):
        m = _OWNER.search(rest[:-1])
        if m:
            owner = _unquote(m.group(1))
    return owner, word


def sources(sql):
    """{alias or table name, lower case: table} of the tables a statement reads."""
    out = {}
    for m in _SOURCES.finditer(sql):
        table = _unquote(m.group(1))
        out[table.lower()] = table
        if m.group(2):
            out[_unquote(m.group(2)).lower()] = table
    return out


def candidates(schema, sql_before, sql_all, word, owner):
    """[(text to insert, label, detail)] for the word being typed.

    schema: {table: [(column, declared type)]}; owner: the name before a '.' (an alias or a
    table) or None. Prefix matches first, then matches inside the name."""
    low = word.lower()
    named = sources(sql_all)
    items = []
    if owner is not None:
        table = named.get(owner.lower()) or next(
            (t for t in schema if t.lower() == owner.lower()), None)
        for col, typ in schema.get(table, ()):
            items.append((quote(col), col, "%s · %s" % (table, (typ or "any").upper())))
    else:
        in_query = [t for t in schema if t.lower() in (v.lower() for v in named.values())]
        for t in in_query:
            for col, typ in schema[t]:
                items.append((quote(col), col, "%s · %s" % (t, (typ or "any").upper())))
        for t in sorted(schema, key=str.lower):
            items.append((quote(t), t, "table · %d columns" % len(schema[t])))
        for k in KEYWORDS:
            items.append((k, k, "keyword"))
        for f in FUNCTIONS:
            items.append((f + "(", f + "()", "function"))
        seen_cols = set(c.lower() for t in in_query for c, _ty in schema[t])
        for t in sorted(schema, key=str.lower):
            if t in in_query:
                continue
            for col, typ in schema[t]:
                if col.lower() not in seen_cols:
                    items.append((quote(col), col, "%s · %s" % (t, (typ or "any").upper())))
        if not low:                     # without a start: only what the statement reads
            items = items[:sum(len(schema[t]) for t in in_query)] or items
    if low:
        first = [i for i in items if i[1].lower().startswith(low)]
        inside = [i for i in items if low in i[1].lower() and i not in first]
        items = first + inside
    out, seen = [], set()
    for i in items:
        if (i[0], i[2]) in seen or (low and i[1].lower() == low and len(items) == 1):
            continue
        seen.add((i[0], i[2]))
        out.append(i)
        if len(out) >= MAX_ITEMS:
            break
    return out


class SqlCompleter(object):
    """Completion for a tk.Text; schema() -> {table: [(column, declared type)]}."""

    def __init__(self, text, schema):
        self.text, self.schema = text, schema
        self.win = None
        self.items = []
        tag = "SqlComplete%d" % id(self)
        text.bindtags((tag,) + text.bindtags())     # ahead of the editor's own keys
        text.bind_class(tag, "<KeyRelease>", self._released)
        text.bind_class(tag, "<KeyPress>", self._pressed)
        text.bind_class(tag, "<Control-space>", lambda e: (self.open(force=True), "break")[1])
        text.bind_class(tag, "<FocusOut>", lambda e: text.after(150, self._focus_check))
        text.bind_class(tag, "<Button-1>", lambda e: self.close())

    # -- the word at the cursor ---------------------------------------------------------------
    def _context(self):
        return word_at(self.text.get("insert linestart", "insert"))

    def _released(self, e):
        if e.keysym in ("Up", "Down", "Return", "Tab", "Escape", "Control_L", "Control_R",
                        "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Left", "Right", "Home",
                        "End", "Prior", "Next"):
            return None
        if e.state & 0x4:               # Control held (copy, paste, run...)
            return None
        owner, word = self._context()
        if e.char == "." or (word and (e.char.isalnum() or e.char == "_")) or (
                self.win is not None and e.keysym == "BackSpace"):
            self.open(force=False)
        else:
            self.close()
        return None

    def _pressed(self, e):
        if self.win is None:
            return None
        if e.keysym in ("Down", "Up"):
            self._move(1 if e.keysym == "Down" else -1)
            return "break"
        if e.keysym in ("Tab", "Return", "KP_Enter"):
            self.take()
            return "break"
        if e.keysym == "Escape":
            self.close()
            return "break"
        return None

    # -- the list -----------------------------------------------------------------------------
    def open(self, force=False):
        owner, word = self._context()
        if not force and not word and owner is None:
            self.close()
            return
        try:
            schema = self.schema() or {}
        except Exception:               # noqa: BLE001 - no database: nothing to offer
            schema = {}
        txt = self.text
        self.items = candidates(schema, txt.get("1.0", "insert"), txt.get("1.0", "end"),
                                word, owner)
        if not self.items:
            self.close()
            return
        if self.win is None:
            self._build()
        lb = self.lb
        lb.delete(0, "end")
        width = max(len(i[1]) for i in self.items)
        for text_, label, detail in self.items:
            lb.insert("end", "%s   %s" % (label.ljust(width), detail))
        lb.configure(height=min(10, len(self.items)),
                     width=min(70, width + 3 + max(len(i[2]) for i in self.items)))
        lb.selection_clear(0, "end")
        lb.selection_set(0)
        lb.activate(0)
        box = txt.bbox("insert")
        if box:
            x = txt.winfo_rootx() + box[0]
            y = txt.winfo_rooty() + box[1] + box[3] + 2
            self.win.geometry("+%d+%d" % (x, y))
        self.win.deiconify()
        self.win.lift()

    def _build(self):
        w = self.win = tk.Toplevel(self.text)
        w.wm_overrideredirect(True)
        w.configure(bg=K["border"])
        self.lb = tk.Listbox(w, font=F["mono"], activestyle="none", exportselection=False,
                             bg=K["card"], fg=K["text"], selectbackground=K["selection"],
                             selectforeground=K["text"], highlightthickness=0, bd=0)
        self.lb.pack(padx=1, pady=1)
        self.lb.bind("<ButtonRelease-1>", lambda e: self.take())

    def _move(self, d):
        cur = self.lb.curselection()
        i = max(0, min(len(self.items) - 1, (cur[0] if cur else -1) + d))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        self.lb.activate(i)
        self.lb.see(i)

    def take(self):
        cur = self.lb.curselection() if self.win is not None else ()
        if not cur:
            self.close()
            return
        insert = self.items[cur[0]][0]
        _owner, word = self._context()
        if word:
            self.text.delete("insert -%dc" % len(word), "insert")
        self.text.insert("insert", insert)
        self.close()
        self.text.focus_set()
        self.text.event_generate("<<Completed>>")

    def _focus_check(self):
        try:
            focus = self.text.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is not self.text and (self.win is None or focus is not self.lb):
            self.close()

    def close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None

    def visible(self):
        return self.win is not None
