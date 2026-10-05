"""Stress: millions of rows scrolled, sorted and filtered at random, reads cancelled in storms,
databases closed under running position builds, multi-megabyte text cells.

Skipped unless SGA_STRESS=1 (they take minutes). SGA_STRESS_ROWS sets the size of the big
table (default 2,000,000); SGA_STRESS_SECONDS how long the grid is driven (default 60). Run
with -X faulthandler: a hang dumps every thread's stack and ends the run.
"""

import bisect
import faulthandler
import gc
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from tests.helpers import ROOT  # noqa: F401  (puts src/ on sys.path)

STRESS = os.environ.get("SGA_STRESS") == "1"
ROWS = int(os.environ.get("SGA_STRESS_ROWS") or 2000000)
SECONDS = float(os.environ.get("SGA_STRESS_SECONDS") or 60)
HANG = 900                 # seconds without finishing a test: dump the stacks and stop


def peak_memory_mb():
    """Peak working set of this process (Windows), else None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return None
    return pmc.PeakWorkingSetSize / 1048576.0


def build_fixture(directory):
    path = os.path.join(directory, "stress.db")
    c = sqlite3.connect(path)
    c.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE big(id INTEGER PRIMARY KEY, n INTEGER, k INTEGER, s TEXT, note TEXT, w TEXT);
        CREATE INDEX big_k ON big(k);
        CREATE TABLE pk(a TEXT, b INTEGER, v, PRIMARY KEY(a, b)) WITHOUT ROWID;
        CREATE TABLE texts(id INTEGER PRIMARY KEY, body TEXT, tag TEXT);
    """)
    rnd = random.Random(7)
    batch = []
    for i in range(ROWS):
        batch.append((None if i % 13 == 0 else rnd.randrange(1000), i % 977,
                      "name %d" % rnd.randrange(50000),
                      None if i % 5 else "note %d %s" % (i, "x" * (i % 60)), "w%d" % (i % 7)))
        if len(batch) == 100000:
            c.executemany("INSERT INTO big(n, k, s, note, w) VALUES (?,?,?,?,?)", batch)
            batch = []
    if batch:
        c.executemany("INSERT INTO big(n, k, s, note, w) VALUES (?,?,?,?,?)", batch)
    c.execute("DELETE FROM big WHERE id % 1000 = 3")
    c.executemany("INSERT INTO pk VALUES (?,?,?)",
                  [("k%05d" % (i % 20000), i, i % 11) for i in range(200000)])
    line = "".join("word%d " % j for j in range(20))
    c.executemany("INSERT INTO texts(body, tag) VALUES (?,?)",
                  [((line + "\n") * (3000 + 9000 * (i % 3)) if i % 4 else line * 20000,
                    "t%d" % (i % 5)) for i in range(40)])
    c.commit()
    c.close()
    return path


@unittest.skipUnless(STRESS, "stress tests run with SGA_STRESS=1")
class StressBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sga_stress_")
        t0 = time.time()
        cls.path = build_fixture(cls.tmp)
        sys.stderr.write("\n[stress] fixture: %s rows in %.1fs, %.0f MB\n"
                         % (format(ROWS, ","), time.time() - t0,
                            os.path.getsize(cls.path) / 1048576.0))

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        faulthandler.dump_traceback_later(HANG, exit=True)
        self.threads_before = set(threading.enumerate())

    def tearDown(self):
        faulthandler.cancel_dump_traceback_later()
        from engine import limits
        limits.reset()

    def assert_no_leaked_threads(self, wait=10.0):
        deadline = time.time() + wait
        while time.time() < deadline:
            extra = [t for t in threading.enumerate()
                     if t not in self.threads_before and t.is_alive()]
            if not extra:
                return
            time.sleep(0.05)
        self.fail("threads left running: %s" % [t.name for t in extra])


class EngineStressTest(StressBase):
    def test_random_windows_equal_offset_windows(self):
        from engine.backends import Filter
        from engine.session import Session
        fast = Session.open(self.path, hash_evidence=False)
        plain = Session.open(self.path, hash_evidence=False)
        rnd = random.Random(1)
        views = [("big", None, False, None), ("big", None, True, None),
                 ("big", "k", False, None), ("big", "k", True, None),
                 ("big", "n", True, None), ("big", "s", False, None),
                 ("big", None, False, Filter(col_exprs={"w": "w3"})),
                 ("big", "n", False, Filter(col_exprs={"n": ">900"})),
                 ("pk", None, False, None), ("pk", "v", True, None)]
        report = []
        try:
            for name, order, desc, flt in views:
                t0 = time.time()
                n = fast.build_positions(name, order, desc, flt)
                built = time.time() - t0
                info = fast.positions_info(name, order, desc, flt)
                worst = 0.0
                points = sorted(set([0, max(0, n - 1)] + [rnd.randrange(max(1, n))
                                                          for _ in range(40)]))
                # the oracle: one pass over the view in its order (as OFFSET reads it) keeping
                # the rows of every window asked for (an OFFSET read per window would take
                # seconds each on millions of sorted rows)
                wanted = {}
                for i, row in enumerate(plain.iter_rows(name, flt, order, desc)):
                    j = bisect.bisect_right(points, i) - 1
                    while j >= 0 and i - points[j] < 50:     # windows may overlap
                        wanted.setdefault(points[j], []).append(row.locator)
                        j -= 1
                for p in points:
                    t0 = time.time()
                    got = fast.browse(name, p, 50, order, desc, flt)
                    worst = max(worst, time.time() - t0)
                    self.assertEqual([r.locator for r in got.rows], wanted.get(p, []),
                                     (name, order, desc, p))
                self.assertEqual(fast.positions_info(name, order, desc, flt)["missed"], 0)
                report.append("%s %s%s%s: %s rows, build %.2fs, %d checkpoints, %s mapped, "
                              "%.1f MB, worst window %.1f ms"
                              % (name, order or "natural", " desc" if desc else "",
                                 " filtered" if flt else "", format(n, ","), built,
                                 info["checkpoints"], format(info["mapped"], ","),
                                 info["bytes"] / 1048576.0, worst * 1000))
        finally:
            fast.close()
            plain.close()
        sys.stderr.write("\n[stress] " + "\n[stress] ".join(report) + "\n")
        self.assert_no_leaked_threads()

    def test_cancel_storm(self):
        from engine.backends import Filter
        from engine.session import Session, is_interrupt
        s = Session.open(self.path, hash_evidence=False)
        stop = threading.Event()
        errors, done = [], [0, 0]
        views = [(None, False, None), ("k", True, None), ("s", False, None),
                 (None, False, Filter(col_exprs={"n": "<100"})), ("n", True, None)]

        def builder():
            rnd = random.Random(2)
            try:
                while not stop.is_set():
                    order, desc, flt = rnd.choice(views)
                    try:
                        s.build_positions("big", order, desc, flt, cancel=stop.is_set)
                        done[0] += 1
                    except sqlite3.OperationalError as e:
                        if not is_interrupt(e):
                            errors.append(repr(e))
                        done[1] += 1
                    try:
                        s.browse("big", rnd.randrange(ROWS), 100, order, desc, flt)
                    except sqlite3.OperationalError as e:
                        if not is_interrupt(e):
                            errors.append(repr(e))
            except Exception as e:      # noqa: BLE001 - reported below
                errors.append(repr(e))
            finally:
                s.release_thread_connection()
        th = threading.Thread(target=builder, name="stress-builder")
        th.start()
        rnd = random.Random(3)
        end = time.time() + min(60, SECONDS)
        storms = 0
        while time.time() < end:
            time.sleep(rnd.random() * (3.0 if rnd.random() < 0.2 else 0.2))
            for _ in range(rnd.randrange(1, 20)):
                s.interrupt(th)
                storms += 1
        stop.set()
        # a browse of a sorted window far into the table is stopped the way the app stops
        # it (an interrupt); on a busy machine one started just before can run long
        deadline = time.time() + 30
        while th.is_alive() and time.time() < deadline:
            s.interrupt(th)
            th.join(0.2)
        self.assertFalse(th.is_alive(), "the builder hangs")
        s.close()
        self.assertEqual(errors, [])
        sys.stderr.write("\n[stress] cancel storm: %d interrupts, %d builds finished, %d stopped\n"
                         % (storms, done[0], done[1]))
        self.assert_no_leaked_threads()

    def test_close_while_building(self):
        from engine.session import Session
        rnd = random.Random(4)
        closes = []
        for i in range(25):
            s = Session.open(self.path, hash_evidence=(i % 5 == 0))
            errors = []

            def work(view):
                try:
                    s.build_positions("big", *view)
                    for _ in range(5):
                        s.browse("big", rnd.randrange(ROWS), 100, view[0], view[1])
                except Exception as e:      # noqa: BLE001 - interrupted or closed: expected
                    errors.append(e)
                finally:
                    s.release_thread_connection()
            threads = [threading.Thread(target=work, args=(v,), name="stress-close-%d" % j)
                       for j, v in enumerate([(None, False), ("s", True), ("k", False)])]
            for th in threads:
                th.start()
            time.sleep(rnd.random() * 0.8)
            t0 = time.time()
            report = s.close()
            closes.append(time.time() - t0)
            for th in threads:
                th.join(30)
                self.assertFalse(th.is_alive(), "a worker hangs after close()")
            self.assertTrue(report.unchanged)
        sys.stderr.write("\n[stress] close under running builds: worst close %.2fs over %d\n"
                         % (max(closes), len(closes)))
        self.assert_no_leaked_threads()


class GridStressTest(StressBase):
    def test_random_scroll_drag_sort_filter_in_the_app(self):
        data_dir = os.path.join(self.tmp, "appdata")
        os.makedirs(data_dir, exist_ok=True)
        os.environ["SGA_DATA_DIR"] = data_dir
        from app import App
        from engine import limits
        app = App()
        app.state("normal")
        app.geometry("1300x800+-4000+0")
        app.tags.warn_with_dialogs = False
        errors = []
        app.report_callback_exception = \
            lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
        g = app._browse_grid
        rnd = random.Random(5)
        state = {"ops": 0, "worst": 0.0, "last": time.time(), "phase": "open"}
        tables = ["big", "texts", "pk"]
        filters = [">500", "NULL", "name 1", "/na(me/", "w2", "<>w3", "5~50", "", "abc"]

        # Everything runs inside mainloop(), as in the app: worker threads hand their results
        # to the Tk thread with after(), which needs the main loop running.
        def tick():
            now = time.time()
            state["worst"] = max(state["worst"], now - state["last"])
            state["last"] = now
            app.after(5, tick)

        def step():
            try:
                if state["phase"] == "open":
                    app._open_db(self.path, wait=True)
                    app._nb.select(app._browse_frame)
                    state["phase"], state["deadline"] = "run", time.time() + SECONDS
                elif state["phase"] == "run":
                    if time.time() >= state["deadline"]:
                        state["phase"], state["settle"] = "settle", time.time() + 60
                    else:
                        random_op()
                        state["ops"] += 1
                elif state["phase"] == "settle":
                    if (not g.loading() and not g.waiting()) or time.time() > state["settle"]:
                        state["phase"] = "done"
                        app.quit()
                        return
            except Exception:           # noqa: BLE001 - reported by the test
                errors.append(__import__("traceback").format_exc())
                app.quit()
                return
            app.after(rnd.randrange(1, 50), step)

        def random_op():
            op = rnd.random()
            if op < 0.30:
                g.drag_to(rnd.random())
            elif op < 0.55:
                g.scroll_rows(rnd.choice((-3, 3, -30, 30, 300)))
            elif op < 0.65:
                g.sort_by(rnd.randrange(len(g.columns())), rnd.random() < 0.5)
            elif op < 0.78:
                cols = g.displayed_columns()[1:]
                if cols:
                    g.set_filter_text(rnd.choice(cols), rnd.choice(filters))
            elif op < 0.83:
                g.clear_filters()
            elif op < 0.86:
                app._browse_table_var.set(rnd.choice(tables))
                app._load_browse_table()
            elif op < 0.90:
                first, end = g.target_row_range()
                if end > first:
                    g.show_cell_tip(first, rnd.choice(g.displayed_columns()), -3000, -3000)
                    g._tip_hide()
            elif op < 0.93:
                limits.load({"limits": {"grid_window_rows": rnd.choice((50, 200, 1000))}})
            else:
                g.yview_moveto(rnd.choice((0.0, 1.0, rnd.random())))
        try:
            app.after(10, tick)
            app.after(300, step)
            app.mainloop()
            self.assertEqual(errors, [])
            self.assertEqual(state["phase"], "done")
            self.assertFalse(g.waiting(), "rows never arrived")
            first, end = g.visible_row_range()
            self.assertEqual(len(g.visible_rows_data()), end - first)
            self.assertEqual(g.load_error, "")
            self.assertEqual(g.frames["empty"], 0, "frames drawn with rows missing")
            self.assertGreater(g.frames["drawn"], 100)
            app._close_db(confirm=False)
        finally:
            try:
                app.destroy()
            except Exception:           # noqa: BLE001
                pass
            from tests.helpers import _KEPT
            _KEPT.append(app)           # freed at exit, on the main thread (helpers.free_tk)
            del app
            gc.collect()
        peak = peak_memory_mb()
        f = g.frames
        sys.stderr.write("\n[stress] grid: %d random operations in %.0fs: %d frames drawn, "
                         "%d kept while reading, %d first-rows placeholders, %d empty; slowest "
                         "event-loop turn %.3fs; peak memory %s MB\n"
                         % (state["ops"], SECONDS, f["drawn"], f["held"], f["first"], f["empty"],
                            state["worst"], "%.0f" % peak if peak is not None else "?"))
        self.assertLess(state["worst"], 5.0, "the window stopped responding")
        if peak is not None:
            self.assertLess(peak, 3000)
        self.assert_no_leaked_threads(20)


if __name__ == "__main__":
    unittest.main()
