"""sqlite_gui_analyzer.py: --version, --self-test (exit code, log, no console) and GUI arguments."""
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import ROOT, TempDirTest

import constants  # noqa: E402
import sqlite_gui_analyzer as entry  # noqa: E402

ENTRY = os.path.join(ROOT, "sqlite_gui_analyzer.py")


def run_entry(*args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, ENTRY] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=300)


def boom():
    raise RuntimeError("kaput")


def not_here():
    raise entry.SkipCheck("does not apply")


class CommandLineTest(TempDirTest):
    def test_version(self):
        r = run_entry("--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "SQLite GUI Analyzer " + constants.VERSION))

    def test_self_test_passes_and_writes_its_log(self):
        log = os.path.join(self.tmp, "self-test.log")
        r = run_entry("--self-test", "--self-test-log", log)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.splitlines(), r.stdout.splitlines())
        for line in ("SQLite GUI Analyzer %s self-test" % constants.VERSION, "PASS  modules",
                     "PASS  tcl", "PASS  engine", "PASS  untrusted",
                     "Self-test passed: 6 checks"):
            self.assertIn(line, text)
        self.assertNotIn("FAIL", text)

    def test_self_test_log_alone_runs_the_self_test(self):
        self.assertTrue(entry.parse_args(["--self-test-log", "x.log"]).self_test)


class SelfTestRunnerTest(TempDirTest):
    def test_a_failed_check_gives_exit_code_1_and_the_traceback(self):
        log = os.path.join(self.tmp, "log.txt")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = entry.self_test(log, [("ok", lambda: "fine"), ("boom", boom), ("skipped", not_here)])
        self.assertEqual(code, 1)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, out.getvalue())
        for line in ("PASS  ok", ": fine", "FAIL  boom", "RuntimeError: kaput",
                     "SKIP  skipped: does not apply", "Self-test FAILED: 1 of 3 checks failed"):
            self.assertIn(line, text)

    def test_works_without_a_console(self):
        # A windowed (console=False) build has sys.stdout = None: the log and exit code remain
        log = os.path.join(self.tmp, "log.txt")
        with mock.patch.object(sys, "stdout", None):
            self.assertEqual(entry.self_test(log, [("ok", lambda: ""), ("skipped", not_here)]), 0)
            self.assertEqual(entry.self_test(None, [("boom", boom)]), 1)
        with open(log, encoding="utf-8") as f:
            self.assertIn("Self-test passed: 2 checks, 1 skipped", f.read())

    def test_an_unwritable_log_is_a_failure_not_a_crash(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(entry.self_test(os.path.join(self.tmp, "missing", "dir", "log.txt"),
                                             [("ok", lambda: "")]), 1)
        self.assertIn("cannot write the log", out.getvalue())

    def test_report_writes_never_raise(self):
        class Broken(object):
            def write(self, *args):
                raise OSError("disk full")
            flush = close = write

        report = entry.Report(os.path.join(self.tmp, "log.txt"))
        report._log.close()
        report._log = Broken()
        with mock.patch.object(sys, "stdout", Broken()):
            report.line("text ✔")
            report.close()
        self.assertEqual(str(report.log_error), "disk full")
        self.assertEqual(report.lines, ["text ✔"])

    def test_tk_check_is_skipped_without_a_display(self):
        with mock.patch.object(entry, "_has_display", return_value=False):
            with self.assertRaises(entry.SkipCheck):
                entry.check_tk()

    def test_built_programs_must_bundle_pillow(self):
        with mock.patch.object(constants, "HAS_PIL", False):
            with self.assertRaises(entry.SkipCheck):
                entry.check_pillow()                          # from source: optional
            with mock.patch.object(sys, "frozen", True, create=True):
                with self.assertRaises(AssertionError):
                    entry.check_pillow()                      # frozen: required

    def test_engine_check_notices_changed_evidence(self):
        from database import DB
        real_close = DB.close

        def close_after_touching(db):
            if db.session is not None:                        # as if something wrote to it
                st = os.stat(db.evidence.main)
                os.utime(db.evidence.main, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
            return real_close(db)

        with mock.patch.object(DB, "close", close_after_touching):
            with self.assertRaises(AssertionError) as cm:
                entry.check_engine()
        self.assertIn("evidence changed", str(cm.exception))
        self.assertIn("modification time", str(cm.exception))


class GuiArgumentsTest(unittest.TestCase):
    def test_database_argument(self):
        self.assertEqual(entry.parse_args(["C:\\cases\\my db.sqlite"]).database, "C:\\cases\\my db.sqlite")
        self.assertIsNone(entry.parse_args([]).database)
        args = entry.parse_args(["--unknown-option", "x.db"])      # unknown options are ignored
        self.assertEqual((args.database, args.self_test, args.version), ("x.db", False, False))

    def test_gui_start_opens_the_database(self):
        with mock.patch("app.App") as app_cls:
            self.assertEqual(entry.main(["evidence.db"]), 0)
        app_cls.assert_called_once_with("evidence.db")
        app_cls.return_value.mainloop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
