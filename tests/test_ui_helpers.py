"""Non-GUI tests of helpers the Tk UI relies on (no window is created)."""
import _tkinter
import os
import threading
import time
import unittest
from types import SimpleNamespace

from tests.helpers import TempDirTest
from engine.schema import Locator

from app import App, WORKER_CALLS_ONLY
from database import BrowseRow
from dialogs import RowWin
from utils import (blob_file_name, create_new_file, export_row_blobs, flag_summary, row_flag_tag,
                   safe_filename)


class BlobExportNameTest(TempDirTest):
    def test_distinct_rows_get_distinct_names(self):
        # WITHOUT ROWID keys that differ only in punctuation, or only past the 80-character
        # limit, used to map to the same file name, so later BLOBs overwrote earlier ones.
        locs = [Locator("pk", ("C:\\x/y",)), Locator("pk", ("C:\\x:y",)),
                Locator("pk", ("k" * 100 + "1",)), Locator("pk", ("k" * 100 + "2",)),
                Locator("rowid", 7), Locator("ordinal", 7)]
        self.assertEqual(safe_filename(str(locs[0])), safe_filename(str(locs[1])))   # the old bug
        names = [blob_file_name("files", loc, "data", ".bin") for loc in locs]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(names[0], blob_file_name("files", Locator("pk", ("C:\\x/y",)), "data", ".bin"))
        for n in names:
            self.assertTrue(n.startswith("files_r") and n.endswith("_data.bin"), n)
            self.assertLess(len(n), 200)

    def test_existing_files_are_never_replaced(self):
        first = os.path.join(self.tmp, "x.bin")
        with open(first, "wb") as f:
            f.write(b"old")
        paths = []
        for payload in (b"new1", b"new2"):
            f, path = create_new_file(self.tmp, "x.bin")
            with f:
                f.write(payload)
            paths.append(path)
        self.assertEqual(len(set(paths + [first])), 3)
        with open(first, "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["x.bin", "x_2.bin", "x_3.bin"])

    def test_export_writes_every_blob_and_keeps_existing_files(self):
        cols = ["_rid", "k", "data"]
        rows = [[Locator("pk", ("C:\\x/y",)), "C:\\x/y", b"one"],
                [Locator("pk", ("C:\\x:y",)), "C:\\x:y", b"two"],
                [Locator("pk", ("e",)), "e", b""],                 # empty BLOB: nothing to write
                [Locator("pk", ("n",)), "n", None]]
        earlier = os.path.join(self.tmp, blob_file_name("files", rows[0][0], "data", ".bin"))
        with open(earlier, "wb") as f:
            f.write(b"earlier export")
        self.assertEqual(export_row_blobs(self.tmp, "files", cols, rows), (2, 0, ""))
        contents = []
        for name in os.listdir(self.tmp):
            with open(os.path.join(self.tmp, name), "rb") as f:
                contents.append(f.read())
        self.assertEqual(sorted(contents), [b"earlier export", b"one", b"two"])


class RowFlagDisplayTest(unittest.TestCase):
    def test_flagged_rows_get_a_tag_and_a_summary(self):
        rows = [BrowseRow([Locator("rowid", 1), None], {"damaged_record"}),
                BrowseRow([Locator("rowid", 2), "x"], {"pre_alter"}),
                BrowseRow([Locator("rowid", 3), "y"], {"virtual_generated"}),   # page note says it
                BrowseRow([Locator("rowid", 4), "z"], {"damaged_record", "pre_alter"}),
                [Locator("rowid", 5), "plain list"]]
        self.assertEqual([row_flag_tag(getattr(r, "flags", ())) for r in rows],
                         ["flag_damaged", "flag_prealter", "", "flag_damaged", ""])
        summary = flag_summary(rows)
        self.assertIn("2 rows: damaged record", summary)
        self.assertIn("2 rows: older than ALTER TABLE", summary)
        self.assertEqual(flag_summary(rows[2:3] + rows[4:]), "")


class BackgroundCountTest(unittest.TestCase):
    """_bg_count runs on a worker thread; the DB can be closed and another opened meanwhile."""

    def test_count_thread_of_a_closed_db_stops_and_never_writes_the_new_cache(self):
        app = SimpleNamespace(_count_gen=1, _count_cache={}, after=lambda *a: None,
                              _after_safe=lambda *a: None, _update_schema_counts=None)
        counted = []

        class FakeDB(object):
            session = object()

            def approx_count(self, t):
                return None

            def count(self, t):
                counted.append(t)
                if t == "a":                  # the user closes this DB and opens another one
                    app._count_gen += 1       # (what _close_db / _open_db do)
                    app._count_cache = {"a": "?"}
                    self.session = object()
                return 5

        app.db = FakeDB()
        old_cache = {}
        App._bg_count(app, ["a", "b", "c"], 1, old_cache)
        self.assertEqual(counted, ["a"])              # stopped after the count in flight
        self.assertEqual(old_cache, {"a": 5})         # ...which went into its own dict
        self.assertEqual(app._count_cache, {"a": "?"})   # the new DB's counts are untouched


class StopWorkersTest(unittest.TestCase):
    """_close_db stops the count, search and SQL-tab threads before the DB closes. While it
    waits, the Tk thread keeps serving the calls those threads make into Tk: a worker blocked in
    after() while the Tk thread blocked in join() would never end."""

    def fake_app(self, target, browse_threads=(), forensics_threads=(), relation_threads=()):
        calls = {"interrupt": 0, "flags": set(), "browse": 0, "forensics": 0, "relations": 0}
        served = threading.Event()

        def dooneevent(flags):
            calls["flags"].add(flags)
            served.set()              # the worker's pending after() call has been served
            return 0

        def cancel_browse():
            calls["browse"] += 1
            return [t for t in browse_threads if t.is_alive()]

        th = threading.Thread(target=target, args=(served,), daemon=True)
        app = SimpleNamespace(
            _count_gen=1, _search_cancel=False, _sql_query_cancel=False,
            _bg_count_thread=None, _search_thread=th, _sql_query_thread=None,
            _cancel_browse_workers=cancel_browse,
            _forensics=SimpleNamespace(
                stop=lambda: calls.__setitem__("forensics", calls["forensics"] + 1),
                worker_threads=lambda: [t for t in forensics_threads if t.is_alive()]),
            tags=SimpleNamespace(worker_threads=lambda: []),
            relations=SimpleNamespace(
                stop=lambda: calls.__setitem__("relations", calls["relations"] + 1),
                worker_threads=lambda: [t for t in relation_threads if t.is_alive()]),
            _timeline=SimpleNamespace(stop=lambda: None, worker_threads=lambda: []),
            # every database of the case is interrupted at once
            case=SimpleNamespace(interrupt=lambda: calls.__setitem__("interrupt",
                                                                     calls["interrupt"] + 1)),
            _count_threads=[],
            datamap=SimpleNamespace(stop=lambda: None, worker_threads=lambda: []),
            tk=SimpleNamespace(dooneevent=dooneevent))
        th.start()
        return app, th, calls

    def test_stops_and_waits_for_a_forensics_job(self):
        # Carving, history and reports run on the Forensics tab's own worker thread
        release = threading.Event()
        self.addCleanup(release.set)
        job = threading.Thread(target=lambda: release.wait(10), daemon=True)
        job.start()
        app, th, calls = self.fake_app(lambda served: served.wait(10), forensics_threads=[job])
        threading.Timer(0.2, release.set).start()          # the job sees the stop and returns
        self.assertEqual(App._stop_workers(app, timeout=5), [])
        self.assertFalse(job.is_alive())
        self.assertEqual(calls["forensics"], 1)

    def test_stops_and_waits_for_a_relations_job(self):
        # Related rows and value checks run on each relationship window's worker thread
        release = threading.Event()
        self.addCleanup(release.set)
        job = threading.Thread(target=lambda: release.wait(10), daemon=True)
        job.start()
        app, th, calls = self.fake_app(lambda served: served.wait(10), relation_threads=[job])
        threading.Timer(0.2, release.set).start()
        self.assertEqual(App._stop_workers(app, timeout=5), [])
        self.assertFalse(job.is_alive())
        self.assertEqual(calls["relations"], 1)

    def test_waits_for_the_browse_grid_workers(self):
        # The Browse grid reads windows and counts rows on worker threads of its own
        release = threading.Event()
        self.addCleanup(release.set)
        grid_worker = threading.Thread(target=lambda: release.wait(10), daemon=True)
        grid_worker.start()
        app, th, calls = self.fake_app(lambda served: served.wait(10), [grid_worker])
        threading.Timer(0.2, release.set).start()          # its interrupted read returns
        self.assertEqual(App._stop_workers(app, timeout=5), [])
        self.assertFalse(grid_worker.is_alive())
        self.assertEqual((calls["browse"], calls["interrupt"]), (1, 1))

    def test_waits_for_a_worker_that_needs_the_tk_thread(self):
        app, th, calls = self.fake_app(lambda served: served.wait(10))   # like a worker in after()
        t0 = time.time()
        self.assertEqual(App._stop_workers(app, timeout=5), [])
        self.assertLess(time.time() - t0, 2)
        self.assertFalse(th.is_alive())
        self.assertEqual((app._count_gen, app._search_cancel, app._sql_query_cancel,
                          calls["interrupt"]), (2, True, True, 1))
        # only the calls worker threads make into Tk: no user input, no timers
        self.assertEqual(calls["flags"], {WORKER_CALLS_ONLY})
        self.assertFalse(WORKER_CALLS_ONLY & (_tkinter.WINDOW_EVENTS | _tkinter.TIMER_EVENTS))

    def test_gives_up_after_the_timeout(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        app, th, _calls = self.fake_app(lambda served: stop.wait(10))    # never finishes by itself
        t0 = time.time()
        self.assertEqual(App._stop_workers(app, timeout=0.3), [th])
        self.assertTrue(0.3 <= time.time() - t0 < 2)


class RowWinPoolKeyTest(unittest.TestCase):
    def test_ordinal_rows_from_different_reads_get_their_own_window(self):
        # '#0' of a sorted view and '#0' of the unsorted view are different rows.
        a = Locator("ordinal", 0, snapshot=(["id"], [1]))
        b = Locator("ordinal", 0, snapshot=(["id"], [100]))
        self.assertNotEqual(RowWin.pool_key("vw", a), RowWin.pool_key("vw", b))
        self.assertEqual(RowWin.pool_key("vw", a), RowWin.pool_key("vw", a))

    def test_keyed_rows_share_a_window(self):
        self.assertEqual(RowWin.pool_key("t", Locator("rowid", 3)),
                         RowWin.pool_key("t", Locator("rowid", 3, (5, 10))))


if __name__ == "__main__":
    unittest.main()
