"""Corpus probe: run the DB layer the UI uses over a corpus and log failures.

Works on temp copies. Checks: tables that browse empty, text/blob/UTF-16 (LE and BE)/hex search
misses (hex both in BLOB/Hex + Deep BLOB and in the strict hex mode), undecoded plists.
Usage: python tools/baseline_probe.py <corpus_root> [out.json]
"""
import sys, os, json, shutil, sqlite3, tempfile, time, re, traceback, binascii, threading

sys.path.insert(0, os.environ.get("SGA_SRC", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")))
from database import DB  # noqa: E402

SIDECARS = ("-wal", "-shm", "-journal")
MAGIC = b"SQLite format 3\x00"


def is_sqlite(p):
    try:
        with open(p, "rb") as f:
            return f.read(16) == MAGIC
    except Exception:
        return False


def ascii_run(b, minlen=6):
    m = re.search(rb"[\x20-\x7e]{%d,}" % minlen, b[1:])  # skip byte 0 so NUL-prefix matters
    return m.group(0)[:12].decode() if m else None


def utf16_run(b, minlen=5):
    m = re.search(rb"(?:[\x20-\x7e]\x00){%d,}" % minlen, b)
    return m.group(0)[: 2 * 10].decode("utf-16-le") if m else None


def utf16be_run(b, minlen=5):
    m = re.search(rb"(?:\x00[\x20-\x7e]){%d,}" % minlen, b)
    return m.group(0)[: 2 * 10].decode("utf-16-be") if m else None


def with_timeout(fn, secs):
    res = {}
    def run():
        try:
            res["v"] = fn()
        except Exception as e:
            res["e"] = repr(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return None, "TIMEOUT"
    return res.get("v"), res.get("e")


def probe(dbfile):
    rep = {"file": dbfile, "issues": [], "tables": 0}
    tmp = tempfile.mkdtemp(prefix="bl_")
    try:
        dst = os.path.join(tmp, os.path.basename(dbfile))
        shutil.copy2(dbfile, dst)
        for s in SIDECARS:
            if os.path.exists(dbfile + s):
                shutil.copy2(dbfile + s, dst + s)
        db = DB()
        t0 = time.time()
        try:
            db.open(dst)
        except Exception as e:
            rep["issues"].append(("OPEN_FAIL", "", repr(e)[:200]))
            return rep
        rep["open_s"] = round(time.time() - t0, 2)
        rep["has_wal"] = db.has_wal
        truth = sqlite3.connect("file:" + dst.replace("\\", "/") + "?mode=ro", uri=True)
        tables = db.tables()
        rep["tables"] = len(tables)
        for t in tables:
            q = '"' + t.replace('"', '""') + '"'
            try:
                n = truth.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
            except Exception as e:
                rep["issues"].append(("COUNT_FAIL", t, repr(e)[:120]))
                continue
            if n == 0:
                continue
            cols, rows = db.browse(t, 3, 0)
            if not rows:
                sql = truth.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0] or ""
                kind = "WITHOUT_ROWID" if "WITHOUT ROWID" in sql.upper() else "OTHER"
                rep["issues"].append(("BROWSE_EMPTY", t, f"{n} rows, {kind}"))
            dcols = db.columns(t)
            # text search probe
            row = truth.execute(f"SELECT * FROM {q} LIMIT 1").fetchone()
            names = [c[0] for c in dcols]
            for ci, v in enumerate(row or []):
                if isinstance(v, str) and len(v) >= 4 and ci < len(names):
                    term = v[:12]
                    res, err = with_timeout(lambda: list(db.search(t, dcols, term, "Case-Insensitive", 5, False, None)), 20)
                    if err or not res:
                        rep["issues"].append(("TEXT_SEARCH_MISS", t, f"{names[ci]} {err or ''}"))
                    break
            # blob probes
            for ci, v in enumerate(row or []):
                if not isinstance(v, bytes) or len(v) < 8 or ci >= len(names):
                    continue
                a = ascii_run(v)
                if a:
                    res, err = with_timeout(lambda: list(db.search(t, dcols, a, "BLOB/Hex", 50, True, None)), 20)
                    if err or not res:
                        rep["issues"].append(("BLOB_ASCII_MISS", t, f"{names[ci]} {err or ''}"))
                u = utf16_run(v)
                if u:
                    res, err = with_timeout(lambda: list(db.search(t, dcols, u, "BLOB/Hex", 50, True, None)), 20)
                    if err or not res:
                        rep["issues"].append(("BLOB_UTF16_MISS", t, f"{names[ci]} {err or ''}"))
                ub = utf16be_run(v)
                if ub:
                    res, err = with_timeout(lambda: list(db.search(t, dcols, ub, "BLOB/Hex", 50, True, None)), 20)
                    if err or not res:
                        rep["issues"].append(("BLOB_UTF16BE_MISS", t, f"{names[ci]} {err or ''}"))
                mid = len(v) // 2
                hx = " ".join(f"{x:02x}" for x in v[mid:mid + 4])
                res, err = with_timeout(lambda: list(db.search(t, dcols, hx, "BLOB/Hex", 50, True, None)), 20)
                if err or not res:
                    rep["issues"].append(("HEX_MISS", t, f"{names[ci]} {err or ''}"))
                res, err = with_timeout(lambda: list(db.search(t, dcols, hx, "hex", 50, False, None)), 20)
                if err or not res:
                    rep["issues"].append(("HEX_MODE_MISS", t, f"{names[ci]} {err or ''}"))
                if v[:6] == b"bplist" or v[:5] == b"<?xml":
                    rep["issues"].append(("PLIST_NOT_DECODED", t, names[ci]))
                break
        if db.has_wal:
            recs, err = with_timeout(lambda: list(db.wal.recover_all_records()), 60)
            rep["wal_records"] = len(recs) if recs is not None else err
        fl, err = with_timeout(db.freed_page_records, 60)
        rep["freed_page_records"] = len(fl) if fl is not None else err
        db.close()
        truth.close()
    except Exception:
        rep["issues"].append(("HARNESS_ERROR", "", traceback.format_exc()[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep


def main():
    root = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "baseline.json"
    files = []
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            if not any(f.endswith(s) for s in SIDECARS) and os.path.getsize(p) >= 512 and is_sqlite(p):
                files.append(p)
    reports = []
    for i, f in enumerate(sorted(files)):
        r, err = with_timeout(lambda: probe(f), 240)
        reports.append(r or {"file": f, "issues": [("PROBE_TIMEOUT_OR_CRASH", "", str(err))]})
        print(f"[{i + 1}/{len(files)}] {os.path.relpath(f, root)}: {len(reports[-1]['issues'])} issues", flush=True)
    json.dump(reports, open(out, "w"), indent=1, default=str)
    from collections import Counter
    c = Counter(iss[0] for r in reports for iss in r["issues"])
    print("\nDBs:", len(reports), "| issue totals:", dict(c))
    print("DBs affected per issue:", {k: sum(1 for r in reports if any(i[0] == k for i in r["issues"])) for k in c})


if __name__ == "__main__":
    main()
