"""What a database's triggers do, in plain words.

A trigger is work the database does by itself: deleting a call deletes its participants
(cascade), inserting a transaction deletes the oldest ones (pruning), a delete writes a line
into a change log, an update keeps a counter, a check refuses a change. For an examiner that
explains rows that are gone without anyone deleting them, and where copies of changed rows
went. Only the CREATE TRIGGER text is read (the trigger is never run); what cannot be parsed
is said, and the SQL itself is always kept.

describe(sql) -> Trigger or None; TriggerMap(entries) answers per table which triggers fire on
it and which change it.
"""

from .schema import unquote_ident
from .sqlsafe import _scan

SHORT = 110                  # characters of a condition shown in a summary (the SQL has all)

DELETES, INSERTS, UPDATES, BLOCKS, CALLS, OTHER = ("deletes", "inserts", "updates", "blocks",
                                                   "calls", "other")
NAMES_SHOWN = 6              # column names listed in a summary before '+N more'
# SQL's own functions (and words written like calls): anything else called is the app's
BUILTIN = frozenset("""
abs avg cast changes char coalesce count date datetime exists glob group_concat hex ifnull iif
in instr json json_extract json_object json_array julianday last_insert_rowid length like lower
ltrim max min not nullif printf quote random randomblob replace round rtrim sqlite_version
strftime substr substring sum time total total_changes trim typeof unicode upper values
zeroblob unixepoch format concat concat_ws octet_length
""".split())


class Action(object):
    """One statement of a trigger body. verb: DELETES / INSERTS / UPDATES / BLOCKS / CALLS
    (a function the app defines: its work is not in the database) / OTHER;
    table: the table it changes (None for BLOCKS / OTHER); where: its WHERE text or '';
    columns: the columns it sets (UPDATE) or fills (INSERT), [] when not named;
    uses_old / uses_new: it reads OLD.x / NEW.x values; raise_kind: IGNORE / ABORT / FAIL /
    ROLLBACK for BLOCKS; sql: the statement."""

    __slots__ = ("verb", "table", "where", "columns", "uses_old", "uses_new", "raise_kind",
                 "message", "sql", "functions")

    def __init__(self, verb, sql, table=None, where="", columns=(), raise_kind=None,
                 message=None, functions=()):
        self.verb, self.sql, self.table, self.where = verb, sql, table, where
        self.columns, self.functions = list(columns), list(functions)
        self.raise_kind, self.message = raise_kind, message
        up = " ".join(sql.upper().split())
        self.uses_old = "OLD." in up.replace(" .", ".")
        self.uses_new = "NEW." in up.replace(" .", ".")


class Trigger(object):
    """A parsed CREATE TRIGGER. timing: BEFORE / AFTER / INSTEAD OF; event: INSERT / DELETE /
    UPDATE; of_columns: UPDATE OF columns ([] = any column); table: the table (or view) it
    is on; when: its WHEN condition or ''; actions: [Action]; source: 'live' or
    'recovered' (a dropped trigger read back from the file)."""

    def __init__(self, name, table, timing, event, of_columns, when, actions, sql,
                 source="live"):
        self.name, self.table, self.timing, self.event = name, table, timing, event
        self.of_columns, self.when, self.actions = list(of_columns), when, list(actions)
        self.sql, self.source = sql, source

    @property
    def kinds(self):
        """What it does, most telling first: 'removes rows' (deletes, by cascade or by
        itself), 'copies or logs rows' (inserts elsewhere using the row's values), 'adds rows',
        'changes rows', 'blocks changes'."""
        out = []
        verbs = [a.verb for a in self.actions]
        if DELETES in verbs:
            out.append("removes rows")
        if any(a.verb == INSERTS and (a.uses_old or a.uses_new) for a in self.actions):
            out.append("copies or logs rows")
        elif INSERTS in verbs:
            out.append("adds rows")
        if UPDATES in verbs:
            out.append("changes rows")
        if BLOCKS in verbs:
            out.append("blocks changes")
        if CALLS in verbs:
            out.append("calls app functions")
        return out

    @property
    def tables_changed(self):
        seen = []
        for a in self.actions:
            if a.table and a.table not in seen:
                seen.append(a.table)
        return seen

    def event_text(self):
        """'When a row is deleted from call_log' and the like."""
        t = self.table
        if self.timing == "INSTEAD OF":
            verb = {"INSERT": "inserting into", "DELETE": "deleting from",
                    "UPDATE": "updating"}.get(self.event, self.event.lower())
            head = "Instead of %s %s" % (verb, t)
        elif self.event == "INSERT":
            head = "When a row is inserted into %s" % t
        elif self.event == "DELETE":
            head = "When a row is deleted from %s" % t
        elif self.of_columns:
            head = "When %s of a row of %s %s" % (
                _join(self.of_columns), t, "changes" if len(self.of_columns) == 1 else "change")
        else:
            head = "When a row of %s is updated" % t
        if self.timing in ("BEFORE", "AFTER"):
            head += " (%s the %s)" % (self.timing.lower(), self.event.lower())
        if self.when:
            head += ", only if %s" % short(self.when)
        return head

    def action_texts(self):
        return [action_text(a, self) for a in self.actions]

    def summary(self):
        """The event, then one line per action."""
        lines = [self.event_text() + ":"]
        lines += ["  - " + t for t in self.action_texts()] or ["  - (does nothing it can say)"]
        return "\n".join(lines)


def short(text, most=SHORT):
    text = " ".join((text or "").split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def _join(names, most=NAMES_SHOWN):
    names = list(names)
    if len(names) <= most:
        return ", ".join(names)
    return "%s +%d more" % (", ".join(names[:most]), len(names) - most)


def action_text(a, trg=None):
    own = trg is not None and a.table is not None and a.table.lower() == trg.table.lower()
    where = (" where %s" % short(a.where)) if a.where else ""
    tname = ("%s itself" % a.table) if own else a.table
    if a.verb == DELETES:
        return ("deletes rows of %s%s" % (tname, where)) if a.where else \
            "deletes every row of %s" % tname
    if a.verb == UPDATES:
        cols = (" (%s)" % _join(a.columns)) if a.columns else ""
        return "changes %s%s%s" % (tname, cols, where)
    if a.verb == INSERTS:
        whose = ""
        if a.uses_old and trg is not None and trg.event in ("DELETE", "UPDATE"):
            whose = ", with values of the %s row" % (
                "deleted" if trg.event == "DELETE" else "old")
        elif a.uses_new:
            whose = ", with values of the new row"
        cols = (" (%s)" % _join(a.columns)) if a.columns else ""
        return "adds a row to %s%s%s" % (tname, cols, whose)
    if a.verb == BLOCKS:
        if a.raise_kind == "IGNORE":
            return "silently skips the change (RAISE IGNORE)"
        msg = (": %s" % short(a.message, 80)) if a.message else ""
        return "refuses the change (RAISE %s)%s" % (a.raise_kind or "", msg)
    if a.verb == CALLS:
        return ("calls %s - a function of the app: what it does is not in the database"
                % _join(["%s()" % f for f in a.functions]))
    return "runs: %s" % short(a.sql)


# -- parsing ------------------------------------------------------------------------------
def _tokens(sql):
    return [(sql[s:e], k, s, e) for s, e, k in _scan(sql)]


def _name(tokens, i):
    """[schema.]name at tokens[i]: (name, next index)."""
    if i >= len(tokens):
        return None, i
    name = unquote_ident(tokens[i][0])
    if i + 2 < len(tokens) and tokens[i + 1][0] == "." and tokens[i + 1][1] == "punct":
        name = unquote_ident(tokens[i + 2][0])
        i += 2
    return name, i + 1


def _up(tok):
    return tok[0].upper() if tok[1] == "word" else None


def describe(sql, source="live"):
    """A Trigger for a CREATE TRIGGER statement, or None when it cannot be read."""
    try:
        return _describe(sql or "", source)
    except (IndexError, ValueError):
        return None


def _describe(sql, source):
    t = _tokens(sql)
    i = 0
    if _up(t[i]) != "CREATE":
        return None
    i += 1
    if _up(t[i]) in ("TEMP", "TEMPORARY"):
        i += 1
    if _up(t[i]) != "TRIGGER":
        return None
    i += 1
    if _up(t[i]) == "IF":
        i += 3                                  # IF NOT EXISTS
    name, i = _name(t, i)
    timing = "BEFORE"
    if _up(t[i]) in ("BEFORE", "AFTER"):
        timing = _up(t[i])
        i += 1
    elif _up(t[i]) == "INSTEAD":
        timing = "INSTEAD OF"
        i += 2
    event = _up(t[i])
    if event not in ("INSERT", "DELETE", "UPDATE"):
        return None
    i += 1
    cols = []
    if event == "UPDATE" and _up(t[i]) == "OF":
        i += 1
        while i < len(t) and _up(t[i]) != "ON":
            if t[i][0] != ",":
                cols.append(unquote_ident(t[i][0]))
            i += 1
    if _up(t[i]) != "ON":
        return None
    table, i = _name(t, i + 1)
    if _up(t[i]) == "FOR":
        i += 3                                  # FOR EACH ROW
    when = ""
    if _up(t[i]) == "WHEN":
        j = i + 1
        depth = 0
        while j < len(t) and not (depth == 0 and _up(t[j]) == "BEGIN"):
            depth += {"(": 1, ")": -1}.get(t[j][0], 0) if t[j][1] == "punct" else 0
            j += 1
        when = sql[t[i + 1][2]:t[j - 1][3]] if j > i + 1 else ""
        i = j
    if _up(t[i]) != "BEGIN":
        return None
    body = t[i + 1:]
    # the body ends at the last END (CASE ... END may come before it)
    last = max((k for k, tok in enumerate(body) if _up(tok) == "END"), default=len(body))
    body = body[:last]
    stmts, cur, depth = [], [], 0
    for tok in body:
        if tok[1] == "punct" and tok[0] in "()":
            depth += 1 if tok[0] == "(" else -1
        if tok[1] == "semi" and depth <= 0:
            if cur:
                stmts.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        stmts.append(cur)
    actions = [_action(sql, s) for s in stmts]
    return Trigger(name, table, timing, event, cols, when, [a for a in actions if a], sql,
                   source)


def _top_level(tokens, start=0):
    """Indexes of the depth-0 tokens from start on."""
    depth = 0
    for k in range(start, len(tokens)):
        tok = tokens[k]
        if tok[1] == "punct" and tok[0] == "(":
            depth += 1
        elif tok[1] == "punct" and tok[0] == ")":
            depth -= 1
        elif depth == 0:
            yield k


def _action(sql, s):
    text = sql[s[0][2]:s[-1][3]]
    k = 0
    if _up(s[0]) == "WITH":                     # a CTE: the statement verb comes after it
        for k in _top_level(s, 1):
            if _up(s[k]) in ("DELETE", "UPDATE", "INSERT", "REPLACE", "SELECT"):
                break
    verb = _up(s[k])
    if verb == "DELETE":
        table, j = _name(s, k + 2)              # DELETE FROM t
        return Action(DELETES, text, table, _where(sql, s, j))
    if verb == "UPDATE":
        j = k + 1
        if _up(s[j]) == "OR":
            j += 2
        table, j = _name(s, j)
        setcols = []
        tops = list(_top_level(s, j))
        for n, idx in enumerate(tops):
            if idx + 1 < len(s) and s[idx + 1][0] == "=" and _up(s[idx]) not in ("WHERE",) and \
                    (n == 0 or s[tops[n - 1]][0] in (",",) or _up(s[tops[n - 1]]) == "SET"):
                setcols.append(unquote_ident(s[idx][0]))
            if _up(s[idx]) in ("WHERE", "FROM"):
                break
        return Action(UPDATES, text, table, _where(sql, s, j), setcols)
    if verb in ("INSERT", "REPLACE"):
        j = k + 1
        while j < len(s) and _up(s[j]) != "INTO":
            j += 1
        table, j = _name(s, j + 1)
        cols = []
        if j < len(s) and s[j][0] == "(":
            j += 1
            while j < len(s) and s[j][0] != ")":
                if s[j][0] != ",":
                    cols.append(unquote_ident(s[j][0]))
                j += 1
        return Action(INSERTS, text, table, "", cols)
    if verb == "SELECT":
        for j, tok in enumerate(s):
            if _up(tok) == "RAISE" and j + 2 < len(s):
                kind = _up(s[j + 2]) or ""
                msg = None
                if j + 4 < len(s) and s[j + 4][1] == "str":
                    msg = s[j + 4][0][1:-1].replace("''", "'")
                return Action(BLOCKS, text, None, "", (), kind, msg)
        called = []
        for j in range(len(s) - 1):
            if s[j][1] in ("word", "qid") and s[j + 1][0] == "(":
                f = unquote_ident(s[j][0])
                if f.lower() not in BUILTIN and f not in called:
                    called.append(f)
        if called:
            return Action(CALLS, text, functions=called)
        return Action(OTHER, text)
    return Action(OTHER, text)


def _where(sql, s, start):
    for k in _top_level(s, start):
        if _up(s[k]) == "WHERE" and k + 1 < len(s):
            return sql[s[k + 1][2]:s[-1][3]]
    return ""


_VERB_WORDS = {DELETES: "deletes rows", UPDATES: "changes rows", INSERTS: "adds rows"}


def _effects(t):
    """What a trigger does, per table it changes: 'backup_changes: deletes rows, adds rows
    (with the deleted row's values)'; blocks and app calls said once."""
    order, per = [], {}
    for a in t.actions:
        if a.table is None:
            continue
        key = a.table
        if key not in per:
            order.append(key)
            per[key] = []
        w = _VERB_WORDS.get(a.verb)
        if a.verb == INSERTS and a.uses_old and t.event in ("DELETE", "UPDATE"):
            w = "adds rows (with the %s row's values)" % (
                "deleted" if t.event == "DELETE" else "old")
        elif a.verb == INSERTS and a.uses_new:
            w = "adds rows (with the new row's values)"
        if w and w not in per[key]:
            per[key].append(w)
    out = []
    for key in order:
        own = key.lower() == (t.table or "").lower()
        out.append("%s: %s" % ("%s itself" % key if own else key, ", ".join(per[key])))
    if any(a.verb == BLOCKS for a in t.actions):
        out.append("can refuse or skip the change")
    calls = [f for a in t.actions if a.verb == CALLS for f in a.functions]
    if calls:
        out.append("calls the app's %s" % _join(["%s()" % f for f in calls]))
    return out


# -- per table -----------------------------------------------------------------------------
class TriggerMap(object):
    """Every trigger of a database (live, and recovered ones given), by table.

    entries: objects with .type, .name, .sql (schema entries); recovered: (name, sql) of
    dropped triggers read back from the file. unreadable lists names whose SQL could not be
    parsed (shown as such, never dropped silently)."""

    def __init__(self, entries, recovered=()):
        self.triggers, self.unreadable = [], []
        for e in entries:
            if getattr(e, "type", None) != "trigger":
                continue
            self._add(e.name, e.sql, "live")
        for name, sql in recovered:
            self._add(name, sql, "recovered")

    def _add(self, name, sql, source):
        t = describe(sql, source)
        if t is None:
            self.unreadable.append((name, source))
        else:
            if t.name != name and name:
                t.name = name
            self.triggers.append(t)

    def named(self, name):
        for t in self.triggers:
            if t.name == name:
                return t
        return None

    def on(self, table):
        """Triggers that fire on table (its inserts, deletes, updates)."""
        low = (table or "").lower()
        return [t for t in self.triggers if (t.table or "").lower() == low]

    def changing(self, table, verb=None):
        """(trigger, action) for every trigger action that changes table (verb: only those
        that, e.g. DELETES)."""
        low = (table or "").lower()
        return [(t, a) for t in self.triggers for a in t.actions
                if (a.table or "").lower() == low and (verb is None or a.verb == verb)]

    def table_text(self, table):
        """Plain lines on what triggers do to and from table, or [] when none involve it."""
        out = []
        for t in self.on(table):
            what = "; ".join(_effects(t)) or "does nothing it can say"
            out.append("%s%s: %s - %s" % (t.name, " [recovered]" if t.source != "live" else "",
                                          t.event_text(), what))
        for t, a in self.changing(table):
            if (t.table or "").lower() == (table or "").lower():
                continue                        # its own trigger, listed above
            out.append("%s%s: %s, it %s" % (t.name, " [recovered]" if t.source != "live" else "",
                                            t.event_text(), action_text(a, t)))
        return out


def for_session(session):
    """TriggerMap of a session: its live triggers, and the dropped ones Forensics has already
    recovered (no scan is started here: until then only the live ones are known)."""
    recovered = []
    fx = getattr(session, "__dict__", {}).get("_forensics")
    cache = getattr(fx, "_cache", None) or {}
    for d in cache.get("dropped") or ():
        if getattr(d, "type", None) == "trigger" and getattr(d, "status", "") == "dropped" \
                and getattr(d, "sql", None):
            recovered.append((d.name, d.sql))
    return TriggerMap(session.schema.entries, recovered)


def tally(tmap):
    """{'total', 'deletes', 'logs', 'blocks', 'calls', 'recovered', 'unreadable'}: how many
    triggers delete rows by themselves, copy or log rows elsewhere, can refuse a change, call
    app functions; how many are recovered (dropped) and unreadable."""
    out = {"total": len(tmap.triggers), "deletes": 0, "logs": 0, "blocks": 0, "calls": 0,
           "recovered": sum(1 for t in tmap.triggers if t.source != "live"),
           "unreadable": len(tmap.unreadable)}
    for t in tmap.triggers:
        verbs = set(a.verb for a in t.actions)
        out["deletes"] += DELETES in verbs
        out["logs"] += any(a.verb == INSERTS and (a.uses_old or a.uses_new) for a in t.actions)
        out["blocks"] += BLOCKS in verbs
        out["calls"] += CALLS in verbs
    out["total"] += out["unreadable"]
    return out


def tally_text(c):
    """'357 triggers: 141 delete rows by themselves, 97 copy or log rows, ...' ('' for none)."""
    if not c["total"]:
        return ""
    bits = []
    for key, words in (("deletes", "delete rows by themselves"), ("logs", "copy or log rows"),
                       ("blocks", "can refuse a change"), ("calls", "call app functions"),
                       ("recovered", "recovered (dropped)"), ("unreadable", "not readable")):
        if c[key]:
            bits.append("%d %s" % (c[key], words))
    return "%d trigger%s%s" % (c["total"], "" if c["total"] == 1 else "s",
                               ": " + ", ".join(bits) if bits else "")
