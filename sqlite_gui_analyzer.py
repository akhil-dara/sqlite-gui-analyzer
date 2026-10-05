#!/usr/bin/env python3
"""SQLite GUI Analyzer - entry point.

Works both as a normal Python script and as a PyInstaller-frozen executable.

    sqlite_gui_analyzer.py [DATABASE]      start the GUI, optionally opening DATABASE
    sqlite_gui_analyzer.py --version       print the version and exit
    sqlite_gui_analyzer.py --self-test [--self-test-log PATH]
        check the installation without opening a window. Exit code 0 = passed, 1 = failed.
        A windowed build has no console, so the exit code is the result; --self-test-log
        also writes the report to PATH.
"""

import os
import sys
import time
import traceback

APP_NAME = "SQLite GUI Analyzer"


def _add_src_to_path():
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(base, "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(prog="sqlite_gui_analyzer", allow_abbrev=False,
                                description=APP_NAME + ": forensic SQLite browser (read-only).")
    p.add_argument("database", nargs="?", help="database to open")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--self-test", action="store_true",
                   help="check the installation without opening a window (exit code 0 = passed)")
    p.add_argument("--self-test-log", metavar="PATH", help="also write the self-test report to PATH")
    args, _unknown = p.parse_known_args(argv)   # the GUI ignores options it does not know
    if args.self_test_log:
        args.self_test = True
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _add_src_to_path()
    if args.version:
        from constants import VERSION
        print("%s %s" % (APP_NAME, VERSION))
        return 0
    if args.self_test:
        return self_test(args.self_test_log)
    from app import App
    app = App(args.database)
    app.mainloop()
    return 0


# -- self-test ---------------------------------------------------------------------------------
class SkipCheck(Exception):
    """A check that does not apply here (e.g. Tk without a display, Pillow not installed)."""


class Report(object):
    """Self-test lines: to stdout when there is one (a windowed build has none) and to the log.
    Writing never raises; a log that could not be written is remembered in log_error."""

    def __init__(self, log_path=None):
        self.lines = []
        self.log_error = None
        self._log = open(log_path, "w", encoding="utf-8") if log_path else None

    def line(self, text):
        self.lines.append(text)
        out = sys.stdout
        if out is not None:
            try:
                try:
                    out.write(text + "\n")
                except UnicodeEncodeError:
                    out.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
                out.flush()
            except (OSError, ValueError, AttributeError):
                pass                 # no usable console
        if self._log is not None:
            try:
                self._log.write(text + "\n")
                self._log.flush()
            except (OSError, ValueError) as e:
                self.log_error = self.log_error or e

    def close(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError as e:
                self.log_error = self.log_error or e
            self._log = None


def run_checks(checks, report):
    """Run (name, function) checks; each returns a detail string or raises. -> (failed, skipped)"""
    failed = skipped = 0
    for name, check in checks:
        t0 = time.time()
        try:
            detail = check()
        except SkipCheck as e:
            skipped += 1
            report.line("SKIP  %s: %s" % (name, e))
            continue
        except Exception:        # noqa: BLE001 - report every failure, never crash the run
            failed += 1
            report.line("FAIL  %s" % name)
            for text in traceback.format_exc().rstrip().splitlines():
                report.line("      " + text)
            continue
        report.line("PASS  %s (%.2fs)%s" % (name, time.time() - t0, ": " + detail if detail else ""))
    return failed, skipped


def self_test(log_path=None, checks=None):
    """Run the self-test; 0 when every check passed or was skipped, else 1. Never raises."""
    try:
        report = Report(log_path)
    except OSError as e:
        Report().line("Self-test FAILED: cannot write the log %s: %s" % (log_path, e))
        return 1
    try:
        _add_src_to_path()
        report.line("%s %s self-test" % (APP_NAME, _version_or_unknown()))
        report.line("Python %s (%d-bit) on %s, %s" % (
            sys.version.split()[0], 64 if sys.maxsize > 2 ** 32 else 32, sys.platform,
            "frozen build" if getattr(sys, "frozen", False) else "from source"))
        report.line("Executable: %s" % sys.executable)
        checks = default_checks() if checks is None else checks
        failed, skipped = run_checks(checks, report)
        if failed:
            report.line("Self-test FAILED: %d of %d checks failed" % (failed, len(checks)))
        else:
            report.line("Self-test passed: %d checks, %d skipped" % (len(checks), skipped))
        result = 1 if failed else 0
    except Exception:            # noqa: BLE001 - a windowed build would show a crash dialog
        report.line("Self-test FAILED: " + traceback.format_exc())
        result = 1
    report.close()
    if report.log_error is not None:
        Report().line("Self-test FAILED: cannot write the log %s: %s" % (log_path, report.log_error))
        return 1
    return result


def _version_or_unknown():
    try:
        from constants import VERSION
        return VERSION
    except Exception:            # noqa: BLE001 - reported by the modules check
        return "(version unknown)"


def default_checks():
    return [("modules", check_modules), ("tcl", check_tcl), ("tk", check_tk),
            ("pillow", check_pillow), ("engine", check_engine), ("untrusted", check_untrusted)]


def check_untrusted():
    """The defences against hostile files work in this build: schema SQL replayed without
    power, the XML reader, the zstd decoder, the lzma memory limit."""
    import lzma
    import struct
    from engine import sqlsafe
    from engine.decode import decode_blob
    from engine.xmlpretty import pretty_xml
    with sqlsafe.Scratch() as scratch:
        try:
            scratch.replay("CREATE TABLE x AS WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL "
                           "SELECT i+1 FROM c) SELECT i FROM c")
            raise AssertionError("a CREATE TABLE ... AS SELECT was run")
        except sqlsafe.ReplayError:
            pass
        scratch.replay("CREATE TABLE t(a INTEGER PRIMARY KEY, b TEXT); SELECT 1")
        _expect([r[1] for r in scratch.pragma("PRAGMA table_info(t)")] == ["a", "b"],
                 "scratch replay")
    try:
        pretty_xml('<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>')
        raise AssertionError("an XML DTD was accepted")
    except ValueError:
        pass
    node = decode_blob(b"\x28\xb5\x2f\xfd\x20\x05\x29\x00\x00hello").children[0]
    _expect(node.kind == "zstd" and node.children[0].value == b"hello", "zstd decode")
    body = lzma.compress(b"x" * 100, format=lzma.FORMAT_ALONE)
    root = decode_blob(body[:1] + struct.pack("<I", 1 << 30) + body[5:])
    _expect("decode_lzma_memory" in root.note, "lzma memory limit: %r" % root.note)
    return "SQLite %s%s" % (sqlite3_version(), " (old: Safe parse advised)"
                            if sqlsafe.old_sqlite() else "")


def sqlite3_version():
    import sqlite3
    return sqlite3.sqlite_version


def _frozen():
    return bool(getattr(sys, "frozen", False))


def check_modules():
    import importlib
    names = ["constants", "engine.session", "engine.backends", "engine.fileformat.wal",
             "database", "wal_parser", "search_results", "utils", "widgets", "dialogs", "app",
             "engine.sqlsafe", "engine.locks", "engine.xmlpretty", "engine.decode.zstd",
             "previews"]
    for name in names:
        importlib.import_module(name)
    import sqlite3
    from constants import VERSION
    return "%d modules, version %s, SQLite %s" % (len(names), VERSION, sqlite3.sqlite_version)


def check_tcl():
    import tkinter
    tcl = tkinter.Tcl()          # the Tcl library files, without a window or a display
    return "Tcl %s" % tcl.eval("info patchlevel")


def _has_display():
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def check_tk():
    """Tk and Pillow's Tk bridge, in a root window that is withdrawn before it is ever shown."""
    if not _has_display():
        raise SkipCheck("no display")
    import tkinter
    from tkinter import ttk
    import constants
    root = tkinter.Tk()
    try:
        root.withdraw()
        themes = ttk.Style(root).theme_names()
        img = tkinter.PhotoImage(master=root, width=4, height=4)
        img.put("#0052cc", to=(0, 0, 4, 4))
        detail = "Tk %s, %d ttk themes" % (root.tk.call("info", "patchlevel"), len(themes))
        if constants.HAS_PIL:
            photo = constants.ImageTk.PhotoImage(constants.PILImage.new("RGB", (4, 4)), master=root)
            if (photo.width(), photo.height()) != (4, 4):
                raise AssertionError("Pillow ImageTk image has the wrong size")
            detail += ", Pillow ImageTk"
        return detail
    finally:
        root.destroy()


def check_pillow():
    """JPEG / WEBP previews need Pillow: optional from source, bundled in the built programs."""
    import io
    import constants
    if not constants.HAS_PIL:
        if _frozen():
            raise AssertionError("Pillow is missing: the built program must bundle it")
        raise SkipCheck("Pillow is not installed (optional: JPEG and WEBP previews)")
    image = constants.PILImage
    done = []
    for fmt in ("PNG", "JPEG", "WEBP"):
        buf = io.BytesIO()
        image.new("RGB", (8, 8), (0, 82, 204)).save(buf, fmt)
        buf.seek(0)
        with image.open(buf) as im:
            im.load()
            if im.size != (8, 8) or im.format != fmt:
                raise AssertionError("%s round trip gave %s %r" % (fmt, im.format, im.size))
        done.append(fmt)
    return "Pillow %s: %s" % (getattr(image, "__version__", "?"), ", ".join(done))


# -- engine round trip -------------------------------------------------------------------------
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PEOPLE, TAGS = 40, 30


def _build_sample(directory):
    """A database with a rowid table holding BLOBs, a WITHOUT ROWID table and a view, plus two
    committed transactions that are only in its WAL. Built in a work folder and copied (with the
    -wal) while the writer is still open, so SQLite cannot checkpoint the WAL away."""
    import shutil
    import sqlite3
    work = os.path.join(directory, "work")
    evidence = os.path.join(directory, "evidence")
    os.mkdir(work)
    os.mkdir(evidence)
    path = os.path.join(work, "selftest.db")
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT, photo BLOB)")
        conn.execute("CREATE TABLE tags(tag TEXT PRIMARY KEY, note TEXT, uses INTEGER) WITHOUT ROWID")
        conn.execute("CREATE VIEW people_named AS SELECT id, name FROM people")
        conn.executemany("INSERT INTO people VALUES (?, ?, ?)", [
            (i, ("needle %03d" if i % 10 == 0 else "person %03d") % i,
             PNG_MAGIC + bytes([i]) * 8 if i % 2 == 0 else None) for i in range(1, PEOPLE + 1)])
        conn.executemany("INSERT INTO tags VALUES (?, ?, ?)", [
            ("tag%02d" % i, "note %d" % i + (" needle" if i % 7 == 0 else ""), i)
            for i in range(TAGS)])
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # the rows above: main file
        conn.execute("INSERT INTO people VALUES (1001, 'needle in the WAL', NULL)")   # WAL only
        conn.execute("INSERT INTO tags VALUES ('wal-tag', 'needle in the WAL', 0)")
        for suffix in ("", "-wal"):
            shutil.copyfile(path + suffix, os.path.join(evidence, "selftest.db" + suffix))
    finally:
        conn.close()
    return os.path.join(evidence, "selftest.db")


def _snapshot(directory):
    import hashlib
    out = {}
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def _expect(condition, message):
    if not condition:
        raise AssertionError(message)


def check_engine():
    """Open a database with a WAL through the UI's DB adapter, browse, search, close, verify."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="sga_selftest_")
    try:
        detail = _engine_round_trip(_build_sample(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _expect(not os.path.exists(tmp), "the temporary folder %s could not be removed" % tmp)
    return detail


def _engine_round_trip(path):
    from database import DB
    from engine.backends import ram_overlay_supported
    from engine.session import MAIN_ONLY, RAM_OVERLAY
    from utils import blob_type

    evidence_dir = os.path.dirname(path)
    before = _snapshot(evidence_dir)
    db = DB()
    db.open(path)
    try:
        want_mode = RAM_OVERLAY if ram_overlay_supported() else MAIN_ONLY
        _expect(db.mode == want_mode, "mode %r, expected %r" % (db.mode, want_mode))
        _expect(db.tables() == ["people", "tags"], "tables %r" % db.tables())
        _expect("people_named" in db.views(), "views %r" % db.views())
        _expect(db.count("people") == PEOPLE + 1 and db.count("tags") == TAGS + 1,
                "counts %r / %r (the WAL rows must be included)" % (db.count("people"), db.count("tags")))

        cols, rows = db.browse("people", 10, 0)
        _expect(cols == ["_rid", "id", "name", "photo"], "people columns %r" % cols)
        _expect([r[1] for r in rows] == list(range(1, 11)), "people ids %r" % [r[1] for r in rows])
        _expect(rows[1][3] == PNG_MAGIC + bytes([2]) * 8 and blob_type(rows[1][3]) == "PNG",
                "BLOB %r" % (rows[1][3],))

        cols, rows = db.browse("tags", 5, 0)                   # WITHOUT ROWID: primary key order
        _expect(cols == ["_rid", "tag", "note", "uses"], "tags columns %r" % cols)
        _expect([r[1] for r in rows] == ["tag00", "tag01", "tag02", "tag03", "tag04"],
                "tags keys %r" % [r[1] for r in rows])
        _expect(rows[0][0].kind == "pk", "WITHOUT ROWID locator %r" % (rows[0][0],))
        data, _cols = db.full_row("tags", rows[0][0])
        _expect(data.get("note") == "note 0 needle", "tags row %r" % dict(data))

        found = {}
        for table, hits, err in db.search_tables(db.tables(), "needle", "Case-Insensitive",
                                                 100, False, None):
            _expect(err is None, "search of %s failed: %r" % (table, err))
            found[table] = set(h["rowid"] for h in hits)
        people = set(loc.value for loc in found.get("people", ()))
        tags = set(loc.value[0] for loc in found.get("tags", ()))
        _expect(people == {10, 20, 30, 40, 1001}, "people hits %r" % sorted(people))
        _expect(tags == {"tag00", "tag07", "tag14", "tag21", "tag28", "wal-tag"},
                "tags hits %r" % sorted(tags))
        rx = set(h["rowid"].value[0] for h in db.search("tags", db.columns("tags"), r"note \d+ needle$",
                                                        "Regex", 100, False, None))
        _expect(rx == tags - {"wal-tag"}, "regex hits %r" % sorted(rx))
        hexhits = set(h["rowid"].value for h in db.search("people", db.columns("people"), "89 50 4E 47",
                                                          "BLOB/Hex", 100, True, None))
        _expect(hexhits == set(range(2, PEOPLE + 1, 2)), "BLOB/Hex hits %r" % sorted(hexhits))

        _expect(db.has_wal and db.wal.summary()["total_frames"] > 0, "no WAL frames")
        wal_hits = list(db.wal.search("needle in the WAL", "Case-Insensitive"))
        _expect(set(h["table"] for h in wal_hits) == {"people", "tags"},
                "WAL hits %r" % [h["table"] for h in wal_hits])
        banners = [b.short for b in db.banners()]
        mode = db.mode
        ev = db.evidence
        _expect(ev.wait_hashing(30), "evidence hashing did not finish: %s" % ev.hash_error)
    finally:
        report = db.close()
    _expect(report is not None and report.unchanged, "evidence changed: %s" % (report and report.differences))
    rehash = ev.verify(rehash=True)
    _expect(rehash.unchanged, "evidence changed: %s" % rehash.differences)
    after = _snapshot(evidence_dir)
    _expect(after == before, "evidence folder changed: %s -> %s" % (sorted(before), sorted(after)))
    return "mode %s (%s); browse, search (text, regex, BLOB/Hex, WAL) and evidence verified" % (
        mode, ", ".join(banners) or "no banners")


if __name__ == "__main__":
    sys.exit(main())
