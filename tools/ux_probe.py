"""UX probe: open a case of many databases off the screen and measure how the window holds it.

usage:
  python tools/ux_probe.py [--count 16] [--size 1200x750] [--json OUT]
      Builds the workspace fixture (tests/fixtures/workspace_fixtures.py, `count` databases)
      in a temporary folder, opens it as one case in the app, placed off the screen (never
      shown, no screen capture), runs a search and builds the timeline, then reports per tab:
        header_px        height of everything above the tabs (header, case bar, chips)
        header_rows      rows of controls above the tabs
        tab              the tab's title (a '· name' suffix shows the active database)
        clipped          controls given less width than they ask for, or outside the window
        max_lines        the most lines one label of the tab takes (status text walls)
        walls            labels taking more than two lines
        longest          the most characters one label shows
  python tools/ux_probe.py --engine-only FOLDER [FOLDER ...] [--recursive]
      No window: scan the folders for SQLite databases and open them read-only through the
      engine (immutable, WAL in RAM) as one case; print only counts, timings and names;
      check that nothing in the folders changed (size and time of every file).
Nothing is ever written next to a database: the fixture goes to a temporary folder and the app
data (tags, settings) to another one.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for p in (SRC, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

OFF_SCREEN = "+-4000+0"
LABELS = ("TLabel", "Label")
CHECKED = ("TButton", "Button", "TCheckbutton", "Checkbutton", "TRadiobutton", "Radiobutton",
           "TLabel", "Label", "TCombobox", "TMenubutton", "Menubutton", "TEntry")


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def in_canvas(w):
    p = w.master
    while p is not None:
        if p.winfo_class() == "Canvas":
            return True
        p = p.master
    return False


def clipped(top, where):
    """[(what, why)] of the controls under `where` that are cut (as tests/test_layout)."""
    from widgets import ElideLabel
    out = []
    tw, tx = top.winfo_width(), top.winfo_rootx()
    for w in walk(where):
        try:
            cls = w.winfo_class()
            if cls not in CHECKED or not w.winfo_ismapped() or in_canvas(w):
                continue
            if isinstance(w, ElideLabel):
                continue
            if cls in LABELS:
                try:
                    wrap = int(float(str(w.cget("wraplength")) or 0))
                except (ValueError, TypeError):
                    wrap = 0
                if wrap > 0 or not str(w.cget("text")).strip():
                    continue
            rw, W = w.winfo_reqwidth(), w.winfo_width()
            name = "%s %r" % (cls, str(w.cget("text"))[:40] if cls not in ("TEntry", "TCombobox")
                              else w.winfo_name())
            if cls == "TEntry":
                if W < 40:
                    out.append((name, "entry only %d px wide" % W))
                continue
            if W + 2 < rw:
                out.append((name, "%d of %d px" % (W, rw)))
                continue
            x = w.winfo_rootx() - tx
            if x + W > tw + 2 or x < -2:
                out.append((name, "outside the window: x %d..%d of %d" % (x, x + W, tw)))
        except Exception:               # noqa: BLE001 - a widget destroyed meanwhile
            continue
    return out


def label_lines(w):
    """Lines a mapped label takes (its height over its font's line height)."""
    import tkinter.font as tkfont
    from tkinter import ttk
    try:
        f = str(w.cget("font") or "")
    except Exception:                   # noqa: BLE001
        f = ""
    if not f and w.winfo_class() == "TLabel":
        style = str(w.cget("style") or "TLabel")
        f = str(ttk.Style(w).lookup(style, "font") or ttk.Style(w).lookup("TLabel", "font") or "")
    try:
        font = tkfont.nametofont(f) if f else tkfont.nametofont("TkDefaultFont")
    except Exception:                   # noqa: BLE001 - a font tuple, not a name
        font = tkfont.Font(font=f) if f else tkfont.nametofont("TkDefaultFont")
    ls = max(1, font.metrics("linespace"))
    return max(1, int(w.winfo_height() // ls))


def text_walls(where, most=2):
    """The texts of the mapped labels under `where` that take more than `most` lines."""
    out = []
    for w in walk(where):
        try:
            if w.winfo_class() not in LABELS or not w.winfo_ismapped():
                continue
            text = str(w.cget("text")).strip()
            if text and label_lines(w) > most:
                out.append("%d lines: %s" % (label_lines(w), text[:80]))
        except Exception:               # noqa: BLE001
            continue
    return out


def text_metrics(where):
    most, walls, longest = 0, 0, 0
    for w in walk(where):
        try:
            if w.winfo_class() not in LABELS or not w.winfo_ismapped():
                continue
            text = str(w.cget("text")).strip()
            if not text:
                continue
            n = label_lines(w)
            most = max(most, n)
            walls += n > 2
            longest = max(longest, len(text))
        except Exception:               # noqa: BLE001
            continue
    return most, walls, longest


def above(app, y_limit):
    """Rows of controls above y_limit (the top of the tabs)."""
    ys = set()
    for w in walk(app):
        try:
            if w.winfo_class() not in CHECKED or not w.winfo_ismapped():
                continue
            top = w.winfo_rooty() - app.winfo_rooty()
            if top + w.winfo_height() <= y_limit:
                ys.add(top // 12)
        except Exception:               # noqa: BLE001
            continue
    # rows closer than 12 px are one row
    rows, last = 0, None
    for y in sorted(ys):
        if last is None or y - last > 1:
            rows += 1
        last = y
    return rows


def keep_off_screen():
    """Every window stays off the screen, message boxes are answered without showing."""
    import re
    import tkinter as tk
    import tkinter.messagebox as mb
    real_geometry, real_init, real_state = tk.Wm.wm_geometry, tk.Toplevel.__init__, tk.Wm.wm_state

    def geometry(self, newGeometry=None):
        if newGeometry is None:
            return real_geometry(self)
        size = re.match(r"^\d+x\d+", str(newGeometry))
        return real_geometry(self, (size.group(0) if size else "") + OFF_SCREEN)

    def init(self, *a, **k):
        real_init(self, *a, **k)
        real_geometry(self, OFF_SCREEN)

    def state(self, newstate=None):
        if newstate == "zoomed":        # never maximised onto the screen
            return None
        return real_state(self, newstate)
    real_tk_init = tk.Tk.__init__

    def tk_init(self, *a, **k):         # the main window off the screen from the start
        real_tk_init(self, *a, **k)
        real_geometry(self, OFF_SCREEN)
    tk.Tk.__init__ = tk_init
    tk.Wm.wm_geometry = tk.Wm.geometry = geometry
    tk.Wm.wm_state = tk.Wm.state = state
    tk.Toplevel.__init__ = init
    for kind in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
                 "askyesnocancel", "askquestion", "askretrycancel"):
        setattr(mb, kind, lambda *a, **k: True)


def probe_app(count, size):
    data = tempfile.mkdtemp(prefix="sga_probe_data_")
    os.environ["SGA_DATA_DIR"] = data
    folder = tempfile.mkdtemp(prefix="sga_probe_case_")
    keep_off_screen()
    from tests.fixtures import workspace_fixtures as wf
    paths = wf.build(folder, count)
    from app import App
    app = App()
    app.tags.warn_with_dialogs = False
    w, h = [int(x) for x in size.split("x")]
    app.geometry("%dx%d" % (w, h))
    errors = []
    app.report_callback_exception = lambda e, v, tb: errors.append("%s: %s" % (e.__name__, v))
    out = {"databases": count, "size": size, "tabs": [], "errors": errors}

    def settle(seconds=0.4, until=None, timeout=60):
        end = time.time() + seconds
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if until is None and time.time() >= end:
                return True
            if until is not None and until():
                app.update()
                return True
            time.sleep(0.01)
        return False

    def run():
        t0 = time.perf_counter()
        opener = getattr(app, "open_case_paths", None)
        (opener or app._open_paths)(paths)
        # the databases open on a worker thread and join the case one by one
        settle(until=lambda: not app.opening(), timeout=300)
        out["open_s"] = round(time.perf_counter() - t0, 2)
        settle(1.0)
        settle(until=lambda: not any(isinstance(v, str) for m in app.case
                                     for v in m.counts.values()), timeout=60)
        # a search, so the Search tab shows its results and status
        app._search_var.set("zebracorn")
        app._do_search()
        settle(until=lambda: not app._search_thread.is_alive(), timeout=60)
        settle(0.5)
        # the timeline: detection, then a build
        tl = app._timeline
        app._nb.select(tl)
        settle(until=lambda: tl.detection is not None and not tl.busy(), timeout=120)
        tl.start_build()
        settle(until=lambda: not tl.busy() and tl.result is not None, timeout=120)
        settle(0.5)
        nb = app._nb
        y_tabs = nb.winfo_rooty() - app.winfo_rooty()
        out["header_px"] = y_tabs
        out["header_rows"] = above(app, y_tabs)
        chrome = [w for w in app.winfo_children() if w is not nb and w.winfo_ismapped()]
        head = []
        for w in chrome:
            try:
                if w.winfo_rooty() - app.winfo_rooty() < y_tabs:
                    head.extend(clipped(app, w))
            except Exception:           # noqa: BLE001
                pass
        out["header_clipped"] = len(set(head))
        side = getattr(app, "_navigator", None) or getattr(app, "_sidebar", None)
        if side is not None:
            out["navigator_px"] = side.winfo_width()
        for tab in nb.tabs():
            nb.select(tab)
            settle(0.5)
            frame = app.nametowidget(tab)
            title = nb.tab(tab, "text").strip()
            cut = clipped(app, frame)
            most, walls, longest = text_metrics(frame)
            out["tabs"].append({"tab": title, "clipped": len(cut), "max_lines": most,
                                "walls": walls, "longest": longest,
                                "clipped_what": sorted(set("%s: %s" % c for c in cut))[:8]})
        app._close_db(confirm=False)
        app.destroy()

    app.after(100, run)
    app.mainloop()
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    return out


def print_report(out):
    print("UX probe: %d databases at %s (opened in %ss)" % (out["databases"], out["size"],
                                                            out.get("open_s")))
    print("  header: %d px, %d row(s) of controls, %d clipped%s" % (
        out.get("header_px", 0), out.get("header_rows", 0), out.get("header_clipped", 0),
        "; navigator %d px" % out["navigator_px"] if "navigator_px" in out else ""))
    print("  %-26s %8s %9s %6s %8s" % ("tab", "clipped", "max_lines", "walls", "longest"))
    for t in out["tabs"]:
        print("  %-26s %8d %9d %6d %8d" % (t["tab"][:26], t["clipped"], t["max_lines"],
                                           t["walls"], t["longest"]))
        for c in t["clipped_what"]:
            print("      cut: %s" % c)
    if out["errors"]:
        print("  Tk errors: %d" % len(out["errors"]))
        for e in out["errors"][:5]:
            print("    " + e[:200])


def snapshot(folder):
    """{relative path: (size, mtime_ns)} of every file under folder."""
    out = {}
    for base, _dirs, files in os.walk(folder):
        for n in files:
            p = os.path.join(base, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, folder)] = (st.st_size, st.st_mtime_ns)
    return out


def engine_only(folders, recursive):
    """Open every SQLite database of the folders read-only through the engine, as one case;
    counts, timings and names only."""
    from case import Case
    from engine.case import scan_folder
    if isinstance(folders, str):
        folders = [folders]
    before = dict((f, snapshot(f)) for f in folders)
    t0 = time.perf_counter()
    found, seen, problems = [], 0, []
    for folder in folders:
        f, s, p = scan_folder(folder, recursive)
        found.extend(f)
        seen += s
        problems.extend(p)
    scan_s = time.perf_counter() - t0
    print("scan: %d SQLite databases in %d files (%.2fs)%s" % (
        len(found), seen, scan_s, "; problems: %d" % len(problems) if problems else ""))
    case = Case()
    t0 = time.perf_counter()
    failed = []
    for c in found:
        t1 = time.perf_counter()
        try:
            m = case.add(c.path)
        except Exception as e:          # noqa: BLE001 - counted, never shown with content
            failed.append((c.name, type(e).__name__))
            continue
        m.load_time = time.perf_counter() - t1
    open_s = time.perf_counter() - t0
    tables = sum(len(m.db.tables()) for m in case)
    print("opened %d of %d (%.2fs), %d tables, failed %d" % (len(case), len(found), open_s,
                                                           tables, len(failed)))
    for name, why in failed:
        print("  not opened: %s (%s)" % (name, why))
    # the navigator's name index: every table and column of every database
    t0 = time.perf_counter()
    try:
        from navigator import NameIndex
        index = NameIndex(list(case))
        build_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        n = 0
        for m in case:
            for t in m.db.tables()[:3]:
                n += len(index.find(t[:5]))
        find_ms = (time.perf_counter() - t0) * 1000.0 / max(1, sum(
            min(3, len(m.db.tables())) for m in case))
        print("name index: %d names in %.3fs; a search takes %.2f ms on average"
              % (len(index), build_s, find_ms))
    except ImportError:
        pass
    modes = {}
    for m in case:
        modes[m.status()] = modes.get(m.status(), 0) + 1
        print("  %-38s %-28s %4d tables  %.3fs" % (m.name[:38], m.status()[:28],
                                                  len(m.db.tables()), m.load_time))
    print("open modes: %s" % ", ".join("%d %s" % (n, k) for k, n in sorted(modes.items())))
    for m in list(case):
        case.remove(m)
    same = all(snapshot(f) == before[f] for f in folders)
    print("folders unchanged: %s (%d files checked)" % (
        same, sum(len(v) for v in before.values())))
    return 0 if same else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--size", default="1200x750")
    ap.add_argument("--json")
    ap.add_argument("--engine-only", metavar="FOLDER", nargs="+")
    ap.add_argument("--recursive", action="store_true")
    a = ap.parse_args(argv)
    if a.engine_only:
        return engine_only(a.engine_only, a.recursive)
    out = probe_app(a.count, a.size)
    print_report(out)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
