"""Time the Browse grid: first rows on screen, scroll redraws and a jump into a large table.

usage: python tools/grid_perf.py [rows]
Builds, in a temporary folder deleted afterwards, a 250-column table (3,000 rows) and a
6-column table of `rows` rows (default 1,000,000), opens each in the app (window placed
off-screen, nothing captured) and reports:
  first rows    time from choosing the table in Browse to its first rows drawn
  redraw        average ms per step over 100 steps (3 rows down, or 60 px right), each step
                followed by update_idletasks(): what the grid itself costs
  with reads    the same steps followed by update(): also delivering windows read meanwhile
  jump          yview_moveto(0.5), then until the rows there are drawn
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from tests.fixtures import make_fixtures as fx   # noqa: E402
from app import App                              # noqa: E402


def big_table(directory, rows):
    path = os.path.join(directory, "big.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE big(id INTEGER PRIMARY KEY, n INTEGER, name TEXT, amount REAL, "
              "data BLOB, note TEXT)")
    batch = []
    for i in range(rows):
        batch.append((i * 7 % 100000, "name %d" % i, i / 3.0, bytes((i % 256,)) * (i % 20),
                      None if i % 11 == 0 else "note %d %s" % (i, "x" * (i % 40))))
        if len(batch) == 50000:
            c.executemany("INSERT INTO big(n, name, amount, data, note) VALUES (?,?,?,?,?)", batch)
            batch = []
    if batch:
        c.executemany("INSERT INTO big(n, name, amount, data, note) VALUES (?,?,?,?,?)", batch)
    c.commit()
    c.close()
    return path


def main():
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 1000000
    tmp = tempfile.mkdtemp(prefix="sga_grid_perf_")
    t0 = time.time()
    dbs = [(fx.wide(tmp), "wide"), (big_table(tmp, rows), "big")]
    print("built fixtures in %.1fs (%s)" % (time.time() - t0, tmp))
    from tools.ux_probe import keep_off_screen
    keep_off_screen()                   # never on the screen, never maximised
    app = App()
    app.state("normal")
    app.geometry("1400x850+-4000+0")
    out = []
    grid = app._browse_grid

    def pump_until(cond, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            if cond():
                return True
            time.sleep(0.002)
        return False

    def loaded():
        first, end = grid.visible_row_range()
        return end > first and all(grid.row_data(r) is not None for r in range(first, end))

    def steps(fn, full):
        t0 = time.perf_counter()
        for _ in range(100):
            fn()
            if full:
                app.update()
            else:
                app.update_idletasks()
        return (time.perf_counter() - t0) * 10       # ms per step

    def run():
        try:
            for path, table in dbs:
                app._open_db(path, wait=True)
                app._nb.select(app._browse_frame)
                app.update()
                app._browse_table_var.set(table)
                t0 = time.perf_counter()
                app._load_browse_table()
                pump_until(loaded)
                first_ms = (time.perf_counter() - t0) * 1000
                # scroll timings without the open's background work (hashing, row counts)
                pump_until(lambda: not grid.loading() and app.db.evidence.hashing_done
                           and not app._bg_count_thread.is_alive()
                           and not app._browse_counter.busy())
                first, end = grid.visible_row_range()
                out.append("%-5s %s rows x %d columns: first rows %.0f ms (%d rows x %d columns "
                           "drawn, %d text items)"
                           % (table, format(grid.row_count(), ","), len(grid.columns()), first_ms,
                              end - first, len(grid.visible_columns()) + 1,
                              len(grid.text_items("cells")) + len(grid.text_items("frozen"))))
                v = steps(lambda: grid.scroll_rows(3), False)
                pump_until(lambda: not grid.loading())
                vr = steps(lambda: grid.scroll_rows(3), True)
                out.append("      vertical   redraw %.1f ms/step, with reads %.1f ms/step" % (v, vr))
                if len(grid.columns()) > 20:
                    grid.xview_moveto(0)
                    app.update()
                    h1 = steps(lambda: grid.scroll_x(60), True)
                    grid.xview_moveto(0)
                    app.update()
                    h = steps(lambda: grid.scroll_x(60), False)
                    grid.xview_moveto(0)
                    app.update()
                    hr = steps(lambda: grid.scroll_x(60), True)
                    out.append("      horizontal redraw %.1f ms/step, with reads %.1f ms/step "
                               "(first pass, creating the filter entries: %.1f ms/step)"
                               % (h, hr, h1))
                t0 = time.perf_counter()
                grid.yview_moveto(0.5)
                pump_until(loaded)
                out.append("      jump to row %s: %.0f ms until drawn"
                           % (format(grid.visible_row_range()[0], ","),
                              (time.perf_counter() - t0) * 1000))
                app._close_db(confirm=False)
        finally:
            app.after(50, app.destroy)

    app.after(500, run)
    app.mainloop()
    shutil.rmtree(tmp, ignore_errors=True)
    print("Python %s" % sys.version.split()[0])
    print("\n".join(out))


if __name__ == "__main__":
    main()
