"""Shared test helpers. Importing this module puts src/ on sys.path."""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

SIDECARS = ("-wal", "-shm", "-journal")


def on_ci():
    """True on a continuous-integration machine (GitHub Actions sets these)."""
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


def within(test, seconds, budget, what=""):
    """A speed check: fails here when `seconds` reaches `budget`; on a CI machine (shared,
    often several times slower) it is a warning on the run instead, never a failure."""
    if seconds < budget:
        return
    msg = "%s took %.2f s (budget %.2f s)" % (what or test.id(), seconds, budget)
    if on_ci():
        print("::warning title=Slower than the budget on this machine::%s" % msg)
        return
    test.fail(msg)


def tk_root(test):
    """A hidden Tk root for a widget test, or skip the test when there is no display.

    The cleanup destroys it and collects it at once, on this thread: a Tk interpreter left in
    a reference cycle would otherwise be freed by whichever thread next triggers the garbage
    collector (a worker thread of a later test), and Tcl aborts when an interpreter is deleted
    by another thread.
    """
    import gc
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        test.skipTest("no display: %s" % e)
    root.withdraw()

    def cleanup():
        from widgets import cancel_all_afters
        cancel_all_afters(root)         # a timer left behind would fire in a later test
        root.destroy()
        _KEPT.append(root)              # freed at exit, on this thread (see free_tk)
        gc.collect()
    test.addCleanup(cleanup)
    return root


_KEPT = []      # destroyed Tk main windows, kept until the tests end (see free_tk)

OFF_SCREEN = "+-4000+0"


def _never_on_screen():
    """For the whole test run: every main window and every Toplevel is created off the screen
    (before it is first shown), and a window asking to be maximised ('zoomed', as the app does
    at start) stays as it is instead. Tests that place windows themselves still can."""
    import tkinter as tk
    if getattr(tk.Tk, "_sga_off_screen", False):
        return
    real_tk_init, real_top_init, real_state = tk.Tk.__init__, tk.Toplevel.__init__, \
        tk.Wm.wm_state

    def tk_init(self, *a, **k):
        real_tk_init(self, *a, **k)
        try:
            self.tk.call("wm", "geometry", self._w, OFF_SCREEN)
        except tk.TclError:
            pass

    def top_init(self, *a, **k):
        real_top_init(self, *a, **k)
        try:
            self.tk.call("wm", "geometry", self._w, OFF_SCREEN)
        except tk.TclError:
            pass

    def state(self, newstate=None):
        if newstate == "zoomed":
            return None
        return real_state(self, newstate)
    tk.Tk.__init__ = tk_init
    tk.Toplevel.__init__ = top_init
    tk.Wm.wm_state = tk.Wm.state = state
    tk.Tk._sga_off_screen = True


_never_on_screen()


def off_screen_windows(test):
    """For the rest of the test every Toplevel opens off the screen and stays there whatever
    position the app asks for (the app places some windows over its main window, others
    where the window manager puts them)."""
    import re
    import tkinter as tk
    real_geometry, real_init = tk.Wm.wm_geometry, tk.Toplevel.__init__

    def geometry(self, newGeometry=None):
        if newGeometry is None:
            return real_geometry(self)
        size = re.match(r"^\d+x\d+", str(newGeometry))
        return real_geometry(self, (size.group(0) if size else "") + OFF_SCREEN)

    def init(self, *a, **k):
        real_init(self, *a, **k)
        real_geometry(self, OFF_SCREEN)

    tk.Wm.wm_geometry = tk.Wm.geometry = geometry
    tk.Toplevel.__init__ = init

    def restore():
        tk.Wm.wm_geometry = tk.Wm.geometry = real_geometry
        tk.Toplevel.__init__ = real_init
    test.addCleanup(restore)


def free_tk(test, attr="app"):
    """Cleanup for a test holding a Tk main window in test.<attr> (already destroyed): keep
    it referenced until the tests end. A worker thread may still hold it for a moment; freed
    by that thread (or by the cycle collector running on it) the Tk interpreter would be
    deleted by the wrong thread, and Tcl aborts ('async handler deleted by the wrong
    thread'). Kept here, it is freed at exit, on the main thread."""
    app = test.__dict__.pop(attr, None)
    if app is not None:
        try:
            if app.winfo_exists():      # a test that never ran the main loop to its end
                from widgets import cancel_all_afters
                cancel_all_afters(app)
                app.destroy()
        except Exception:               # noqa: BLE001 - already destroyed
            pass
        _KEPT.append(app)


class TempDirTest(unittest.TestCase):
    """Gives each test a fresh temporary directory in self.tmp."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sga_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def copy_with_sidecars(src, directory, name=None, sidecars=SIDECARS):
    """Copy a database and its sidecar files (default: -wal/-shm/-journal) into directory."""
    dst = os.path.join(directory, name or os.path.basename(src))
    shutil.copyfile(src, dst)
    for sfx in sidecars:
        if os.path.exists(src + sfx):
            shutil.copyfile(src + sfx, dst + sfx)
    return dst


def lenient(raw):
    from engine.fileformat.record import InvalidText
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return InvalidText(raw)


def norm(value):
    """Type-tagged value for exact comparisons (1 != 1.0, str != bytes)."""
    from engine.fileformat.record import InvalidText
    if isinstance(value, InvalidText):
        return ("invalid", bytes(value))
    if isinstance(value, bytes):
        return ("blob", value)
    if isinstance(value, float):
        return ("real", repr(value))
    return (type(value).__name__, value)


def oracle_rows(db_path, table, columns, with_rowid, collations=()):
    """Rows as SQLite sees them, read from a throw-away copy (SQLite may apply the WAL there)."""
    from engine.schema import quote_ident, register_collations
    tmp = tempfile.mkdtemp(prefix="sga_oracle_")
    try:
        copy = copy_with_sidecars(db_path, tmp)
        conn = sqlite3.connect(copy)
        try:
            conn.text_factory = lenient
            register_collations(conn, collations)
            sel = ", ".join(quote_ident(c) for c in columns)
            if with_rowid:
                sel = "_rowid_, " + sel
            rows = conn.execute("SELECT %s FROM %s" % (sel, quote_ident(table))).fetchall()
        finally:
            conn.close()        # before the copy is removed: an open file blocks that on Windows
        return sorted(tuple(norm(v) for v in r) for r in rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_TCL_SPECIAL = set(' {}[]"$\\;\t\n')


def _tcl_word(value):
    """value as one Tcl word, the way Tk quotes a %-substitution."""
    value = str(value)
    if value == "":
        return "{}"
    if not any(ch in _TCL_SPECIAL for ch in value):
        return value
    if "\\" not in value and value.count("{") == value.count("}") and "{" not in value:
        return "{%s}" % value
    return "".join("\\" + ch if ch in _TCL_SPECIAL else ch for ch in value)


def _key_bindings(widget, tag):
    """[(sequence, modifiers, keysym or None)] of the key bindings of one bind tag."""
    out = []
    for seq in widget.tk.splitlist(widget.tk.call("bind", tag)):
        seq = str(seq)
        if not seq.startswith("<") or seq.startswith("<<"):
            continue
        parts = seq[1:-1].split("-")
        kinds = [p for p in parts if p in ("Key", "KeyPress")]
        if not kinds:
            continue
        i = parts.index(kinds[0])
        mods, rest = parts[:i], parts[i + 1:]
        out.append((seq, frozenset(mods), rest[0] if rest else None))
    return out


def send_key(widget, keysym, char="", modifiers=()):
    """A key press delivered to `widget` as Tk would deliver it to the focused widget: the
    best-matching key binding of each of its bind tags in turn (a specific key before <Key>,
    more modifiers first), stopped by 'break'. Unlike event_generate it does not need the
    application to have the operating system's keyboard focus (another program taking it,
    e.g. a second test run, made key tests fail)."""
    mods = frozenset(modifiers)
    state = {"Shift": 1, "Control": 4, "Alt": 131072}
    fields = {"W": str(widget), "K": keysym, "A": char, "k": 0, "N": 0, "x": 0, "y": 0,
              "X": 0, "Y": 0, "#": 0, "T": 2, "E": 1, "s": sum(state.get(m, 0) for m in mods),
              "b": "??", "d": "??", "D": "??", "f": 0, "h": "??", "w": "??", "t": 0, "i": 0,
              "M": 0, "R": "??", "S": "??", "B": "??", "P": "??", "c": 0, "m": "??", "o": 0,
              "p": "??", "a": "??", "%": "%"}
    for tag in widget.bindtags():
        best = None
        for seq, bmods, detail in _key_bindings(widget, tag):
            if detail not in (None, keysym) or not bmods <= mods:
                continue
            score = (detail is not None, len(bmods))
            if best is None or score > best[0]:
                best = (score, seq)
        if best is None:
            continue
        script = str(widget.tk.call("bind", tag, best[1]))
        out, i = [], 0
        while i < len(script):
            ch = script[i]
            if ch == "%" and i + 1 < len(script):
                f = script[i + 1]
                out.append("%" if f == "%" else _tcl_word(fields.get(f, "??")))
                i += 2
                continue
            out.append(ch)
            i += 1
        try:
            widget.tk.eval("".join(out))
        except Exception as e:          # 'break' ends the dispatch like in Tk
            if "break" in str(e) or 'invoked "break"' in str(e):
                return
            raise
    return


def focused(widget):
    """The widget with the keyboard focus in widget's application, or, while another program
    has the operating system's focus, the one that gets it back (Tk remembers it)."""
    f = widget.focus_get()
    if f is not None:
        return str(f)
    return str(widget.tk.call("focus", "-lastfor", widget))


def dir_snapshot(directory):
    """{name: (size, mtime_ns, sha256)} for every file in a directory."""
    out = {}
    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        if os.path.isfile(p):
            st = os.stat(p)
            with open(p, "rb") as f:
                out[name] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    return out
