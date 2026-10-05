"""Measure Browse window latency before/after position indexes, and a scripted scrollbar drag.

usage: python tools/scroll_perf.py DB TABLE [--sort COL] [--unindexed COL] [--filter COL:EXPR]
                                           [--table TABLE ...] [--drags N] [--no-app]

For each table: the time of one 200-row window at 0 / 25 / 50 / 75 % and at the end, read with
LIMIT/OFFSET (a session without position indexes) and through a position index (after it is
built), in natural order, sorted by --sort (an indexed column), sorted by --unindexed and
filtered by --filter (engine.filters syntax, e.g. "status:=5" or "body:/a/");
the index build time and its memory. Then (unless --no-app) the app opens the database, and
the Browse grid of each table is dragged to N random scrollbar positions (grid methods and the
event loop only, nothing captured on screen): frames drawn with rows missing (must be 0),
frames that kept the previous rows while reading, and the time until the rows at the new
position were drawn. The database is opened read-only through the engine; only names, counts
and timings are printed.
"""

import argparse
import os
import random
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from engine.backends import Filter           # noqa: E402
from engine.session import Session           # noqa: E402

WINDOW = 200
POINTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def views_for(spec):
    out = [("natural", None, None)]
    if spec.sort:
        out.append(("sorted by %s (indexed)" % spec.sort, spec.sort, None))
    if spec.unindexed:
        out.append(("sorted by %s (no index)" % spec.unindexed, spec.unindexed, None))
    if spec.filter:
        col, expr = spec.filter.split(":", 1)
        out.append(("filtered %s" % col, None, Filter(col_exprs={col: expr})))
    return out


def window_times(s, table, order, flt, n):
    times = []
    for f in POINTS:
        p = max(0, min(n - WINDOW, int(n * f)))
        t0 = time.perf_counter()
        s.browse(table, p, WINDOW, order, False, flt)
        times.append((time.perf_counter() - t0) * 1000)
    return times


def fmt(times):
    return " / ".join("%.0f" % t if t >= 10 else "%.1f" % t for t in times)


def measure_engine(path, specs):
    for spec in specs:
        plain = Session.open(path, hash_evidence=False)
        fast = Session.open(path, hash_evidence=False)
        try:
            total = plain.count(spec.table)
            print("\n%s: %s rows (window %d rows; ms at %s)"
                  % (spec.table, format(total, ","), WINDOW,
                     " / ".join("%d%%" % (f * 100) for f in POINTS)))
            for label, order, flt in views_for(spec):
                n = plain.count(spec.table, flt)
                before = window_times(plain, spec.table, order, flt, n)
                t0 = time.perf_counter()
                fast.build_positions(spec.table, order, False, flt)
                built = time.perf_counter() - t0
                info = fast.positions_info(spec.table, order, False, flt)
                after = window_times(fast, spec.table, order, flt, n)
                info2 = fast.positions_info(spec.table, order, False, flt)
                print("  %-34s rows %s" % (label, format(n, ",")))
                print("    OFFSET  : %s" % fmt(before))
                print("    indexed : %s   (build %.2f s, %s checkpoints every %s, %s rows mapped, "
                      "%.1f MB; served %d, missed %d)"
                      % (fmt(after), built, format(info["checkpoints"], ","),
                         format(info["every"], ","), format(info["mapped"], ","),
                         info["bytes"] / 1048576.0, info2["served"], info2["missed"]))
        finally:
            plain.close()
            fast.close()


def measure_app(path, specs, drags):
    os.environ.setdefault("SGA_DATA_DIR", tempfile.mkdtemp(prefix="sga_perf_"))
    from app import App
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.ux_probe import keep_off_screen
    keep_off_screen()                   # never on the screen, never maximised
    app = App()
    app.state("normal")
    app.geometry("1400x850+-4000+0")
    app.tags.warn_with_dialogs = False
    g = app._browse_grid
    rnd = random.Random(11)
    out = []
    plan = []
    for spec in specs:
        plan.append((spec.table, "during index build"))
        plan.append((spec.table, "index built"))
    state = {"i": -1, "k": 0, "t0": None, "lat": [], "busy": None}

    def start_next():
        state["i"] += 1
        if state["i"] >= len(plan):
            app.quit()
            return
        table, phase = plan[state["i"]]
        if phase == "during index build":
            app._browse_table_var.set(table)
            app._load_browse_table()
        state.update(k=0, lat=[], busy=app._browse_pos_busy, frames=dict(g.frames))
        wait_ready(phase)

    def wait_ready(phase):
        # 'index built': wait for the Browse position index; 'during': start at once
        if phase == "index built" and app._browse_pos_busy:
            app.after(50, lambda: wait_ready(phase))
            return
        if g.loading() or g.waiting():
            app.after(10, lambda: wait_ready(phase))
            return
        state["frames"] = dict(g.frames)
        drag()

    def drag():
        if state["k"] >= drags:
            finish()
            return
        state["k"] += 1
        g.drag_to(rnd.random())
        state["t0"] = time.perf_counter()
        app.after(1, settle)

    def settle():
        # drawn: the rows at the thumb's position are on screen (not the rows kept meanwhile)
        drawn = not g.waiting() and g.visible_row_range()[0] == g._top
        if not drawn:
            if time.perf_counter() - state["t0"] > 60:
                state["lat"].append(60000.0)
                app.after(1, drag)
                return
            app.after(1, settle)
            return
        state["lat"].append((time.perf_counter() - state["t0"]) * 1000)
        app.after(rnd.randrange(5, 40), rest)

    def rest():
        if g.loading():                 # windows read ahead: let them finish between drags
            app.after(2, rest)
            return
        drag()

    def finish():
        table, phase = plan[state["i"]]
        before, now = state["frames"], dict(g.frames)
        d = dict((k, now.get(k, 0) - before.get(k, 0)) for k in ("drawn", "held", "empty"))
        lat = sorted(state["lat"])
        out.append("  %s, %s: %d drags; frames drawn %d, kept previous rows %d, rows missing %d;"
                   " until drawn: median %.0f ms, p95 %.0f ms, max %.0f ms"
                   % (table, phase, len(lat), d["drawn"], d["held"], d["empty"],
                      lat[len(lat) // 2], lat[int(len(lat) * 0.95) - 1], lat[-1]))
        app.after(10, start_next)

    def begin():
        app._open_db(path, wait=True)
        app._nb.select(app._browse_frame)
        app.after(200, start_next)
    app.after(300, begin)
    app.mainloop()
    app._close_db(confirm=False)
    app.destroy()
    print("\nscrollbar drag through the Browse grid (window %d rows):" % WINDOW)
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("table")
    ap.add_argument("--sort")
    ap.add_argument("--unindexed")
    ap.add_argument("--filter")
    ap.add_argument("--table", dest="more", action="append", default=[],
                    help="another table: NAME[,sort[,unindexed[,COL:EXPR]]]")
    ap.add_argument("--drags", type=int, default=60)
    ap.add_argument("--no-app", action="store_true")
    a = ap.parse_args()
    specs = [argparse.Namespace(table=a.table, sort=a.sort, unindexed=a.unindexed,
                                filter=a.filter)]
    for m in a.more:
        parts = (m.split(",") + [None] * 4)[:4]
        specs.append(argparse.Namespace(table=parts[0], sort=parts[1] or None,
                                        unindexed=parts[2] or None, filter=parts[3] or None))
    measure_engine(a.db, specs)
    if not a.no_app:
        measure_app(a.db, specs, a.drags)


if __name__ == "__main__":
    main()
