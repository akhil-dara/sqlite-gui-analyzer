"""Open a (large) database read-only and time the first Browse pages; verify it is untouched.

usage: python tools/perf_check.py <database> [table]
Opens the file in place with the engine (immutable, nothing written) and hashes it in the
background, exactly as the app does.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from engine.session import Session  # noqa: E402


def main():
    path = sys.argv[1]
    folder = os.path.dirname(os.path.abspath(path))
    st0, list0 = os.stat(path), sorted(os.listdir(folder))
    t0 = time.time()
    s = Session.open(path)
    t_open = time.time() - t0
    table = sys.argv[2] if len(sys.argv) > 2 else max(s.tables(), key=lambda t: s.info(t).root_page)
    t1 = time.time()
    page = s.browse(table, 0, 200)
    t_first = time.time() - t1
    print("mode=%s tables=%d open=%.2fs first page of %s=%.2fs (%d rows, %s)"
          % (s.mode, len(s.tables()), t_open, table, t_first, len(page.rows), page.source))
    print("hashed so far: %.0f of %.0f MB" % (s.evidence.hashed_bytes / 1e6, s.evidence.total_bytes / 1e6))
    report = s.close()
    st1, list1 = os.stat(path), sorted(os.listdir(folder))
    print(report.text().encode("ascii", "replace").decode())
    print("mtime same:", st0.st_mtime_ns == st1.st_mtime_ns, "| size same:", st0.st_size == st1.st_size,
          "| folder listing same:", list0 == list1)
    ok = t_open < 2 and t_first < 1 and list0 == list1 and st0.st_mtime_ns == st1.st_mtime_ns
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
