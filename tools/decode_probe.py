"""Run the BLOB decoders over every BLOB of a corpus of SQLite files and report.

usage: python tools/decode_probe.py <corpus_root> [--rows N] [--max-blob BYTES] [--json OUT]

Every file except -wal/-shm/-journal sidecars and files under 512 bytes is offered to the
engine (Session, immutable, nothing written); files it refuses (not SQLite) are counted and
skipped. At most N rows per table are read (default 300) and BLOBs larger than --max-blob
(default 8 MB) are skipped. For every BLOB, decode_blob(), summary() and decoded_strings()
run. Reported: exceptions and contained decoder errors (must be 0), counts per top
interpretation, the slowest BLOBs, BLOBs with a plist signature that did not decode as a
plist, and typedstream text extraction. --json must point outside the corpus folder.
"""

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from engine.decode import decode_blob, decoded_strings, summary  # noqa: E402
from engine.decode.detect import magic  # noqa: E402
from engine.session import Session  # noqa: E402

SIDECARS = ("-wal", "-shm", "-journal")


def candidate_files(root):
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(folder, name)
            if name.endswith(SIDECARS) or name.upper() == "MANIFEST.JSON":
                continue
            try:
                if os.path.getsize(path) < 512:
                    continue
            except OSError:
                continue
            yield path


def chain_of(root):
    """'gzip>protobuf' style chain of the best interpretation."""
    kinds, node = [], root
    for _ in range(6):
        if not node.children:
            break
        best = node.children[0]
        kinds.append(best.kind + ("" if best.confidence == "confident" else "?"))
        if best.children and best.children[0].kind == "bytes" and best.kind in (
                "gzip", "zlib", "deflate", "bz2", "xz", "lzma", "zstd", "base64"):
            node = best.children[0]
            continue
        break
    return ">".join(kinds) or "none"


class Report(object):
    def __init__(self):
        self.files = self.dbs = self.tables = self.blobs = self.skipped_big = 0
        self.open_errors = []
        self.table_errors = []
        self.exceptions = []
        self.kinds = {}
        self.slowest = []
        self.plist_failures = []
        self.typedstream = [0, 0]           # blobs, blobs with text extracted
        self.strings = 0
        self.total_time = 0.0

    def blob(self, where, data):
        self.blobs += 1
        t0 = time.perf_counter()
        try:
            root = decode_blob(data)
            line = summary(data)
            strings = decoded_strings(data)
        except Exception:       # noqa: BLE001 - the API promises never to raise
            self.exceptions.append((where, len(data), traceback.format_exc()))
            return
        elapsed = time.perf_counter() - t0
        self.total_time += elapsed
        note = root.note or ""
        if "decode error" in note or "decoder error" in note or line.startswith("undecodable"):
            self.exceptions.append((where, len(data), note or line))
        chain = chain_of(root)
        self.kinds[chain] = self.kinds.get(chain, 0) + 1
        self.strings += len(strings)
        self.slowest.append((elapsed, where, len(data), line))
        self.slowest.sort(key=lambda x: -x[0])
        del self.slowest[15:]
        kind = magic(data)[0]
        if kind in ("bplist", "xml_plist"):
            if not any(c.kind == kind for c in root.children):
                self.plist_failures.append((where, len(data), data[:16].hex(), line))
        if kind == "typedstream":
            self.typedstream[0] += 1
            ts = root.find("typedstream")
            if ts is not None and ts.value:
                self.typedstream[1] += 1


def probe_file(path, rep, rows_per_table, max_blob):
    rep.files += 1
    try:
        session = Session(path, hash_evidence=False)
    except Exception as e:      # noqa: BLE001 - not SQLite, encrypted, damaged...
        rep.open_errors.append((path, "%s: %s" % (type(e).__name__, e)))
        return
    rep.dbs += 1
    try:
        for table in session.tables():
            rep.tables += 1
            rows = session.iter_rows(table)
            try:
                for n, row in enumerate(rows):
                    if n >= rows_per_table:
                        break
                    for ci, value in enumerate(row.values):
                        if isinstance(value, bytes) and value:
                            if len(value) > max_blob:
                                rep.skipped_big += 1
                                continue
                            rep.blob((path, table, ci, n), bytes(value))
            except Exception as e:  # noqa: BLE001 - a table the engine cannot read
                rep.table_errors.append((path, table, "%s: %s" % (type(e).__name__, e)))
            finally:
                rows.close()
    finally:
        session.close()


def safe_output(stream):
    """The report stream, never failing on a character the console cannot show (a cp1252
    console met a table or value name it cannot encode after the whole corpus was probed):
    such characters are written as backslash escapes."""
    try:
        stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    return stream


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--rows", type=int, default=300)
    ap.add_argument("--max-blob", type=int, default=8 * 1000 * 1000)
    ap.add_argument("--json")
    args = ap.parse_args()
    if args.json:
        corpus = os.path.normcase(os.path.abspath(args.root))
        target = os.path.normcase(os.path.abspath(args.json))
        if target == corpus or target.startswith(corpus + os.sep):
            ap.error("--json must be outside the corpus folder")
    rep = Report()
    t0 = time.time()
    for path in candidate_files(args.root):
        probe_file(path, rep, args.rows, args.max_blob)
    wall = time.time() - t0
    rel = lambda p: os.path.relpath(p, args.root)  # noqa: E731
    out = safe_output(sys.stdout)
    out.write("files tried %d, SQLite opened %d, tables %d, BLOBs %d (skipped > max: %d)\n"
              % (rep.files, rep.dbs, rep.tables, rep.blobs, rep.skipped_big))
    out.write("decode time %.1fs of %.1fs wall; decoded strings %d\n"
              % (rep.total_time, wall, rep.strings))
    out.write("EXCEPTIONS: %d\n" % len(rep.exceptions))
    for where, size, text in rep.exceptions[:20]:
        out.write("  %s %s col %d row %d (%d B)\n%s\n" % (rel(where[0]), where[1], where[2],
                                                          where[3], size, text))
    out.write("plist-signature BLOBs not decoded as plist: %d\n" % len(rep.plist_failures))
    for where, size, head, line in rep.plist_failures[:50]:
        out.write("  %s %s col %d row %d (%d B) %s | %s\n"
                  % (rel(where[0]), where[1], where[2], where[3], size, head, line))
    out.write("typedstream BLOBs %d, with text extracted %d\n" % tuple(rep.typedstream))
    out.write("table read errors (engine, not decoders): %d\n" % len(rep.table_errors))
    for path, table, text in rep.table_errors[:10]:
        out.write("  %s %s: %s\n" % (rel(path), table, text[:160]))
    out.write("top interpretation counts:\n")
    for chain, n in sorted(rep.kinds.items(), key=lambda kv: (-kv[1], kv[0])):
        out.write("  %7d  %s\n" % (n, chain))
    out.write("slowest BLOBs:\n")
    for elapsed, where, size, line in rep.slowest:
        out.write("  %.3fs %s %s col %d (%d B) %s\n" % (elapsed, rel(where[0]), where[1],
                                                        where[2], size, line))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"blobs": rep.blobs, "exceptions": len(rep.exceptions),
                       "plist_failures": [(rel(w[0]), w[1], w[2], w[3], s, h, l)
                                          for w, s, h, l in rep.plist_failures],
                       "typedstream": rep.typedstream, "kinds": rep.kinds,
                       "open_errors": len(rep.open_errors),
                       "table_errors": len(rep.table_errors)}, f, indent=1)
    sys.exit(1 if rep.exceptions else 0)


if __name__ == "__main__":
    main()
