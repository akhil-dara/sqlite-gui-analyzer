"""Whole-corpus checks. Set SQLITE_CORPUS to a folder of SQLite test databases.

Each database is copied to a temp folder first (test data, not evidence), then:
  * native rows must equal SQLite rows for every B-tree table SQLite can read;
  * no table may browse empty while it has rows unless the page explains why;
  * the copy's folder must be byte-identical after the session closes.
Anti-forensic test files named 13-*, 14-*, 19-* (manipulated records) are excluded from the
equality check: SQLite's behaviour on corrupt records is not ground truth.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from tests.helpers import copy_with_sidecars, dir_snapshot, lenient, norm
from engine.schema import quote_ident, register_collations
from engine.session import Session, SessionError

CORPUS = os.environ.get("SQLITE_CORPUS")
MAGIC = b"SQLite format 3\x00"
EXCLUDED = ("13-0", "14-0", "19-0")


def corpus_files():
    for d, _, names in os.walk(CORPUS):
        for n in names:
            p = os.path.join(d, n)
            if n.endswith(("-wal", "-shm", "-journal")) or os.path.getsize(p) < 512:
                continue
            with open(p, "rb") as f:
                if f.read(16) == MAGIC:
                    yield p


@unittest.skipUnless(CORPUS, "set SQLITE_CORPUS to run corpus tests")
class CorpusTest(unittest.TestCase):
    def test_corpus(self):
        failures, tables_checked = [], 0
        for src in sorted(corpus_files()):
            tmp = tempfile.mkdtemp(prefix="sga_corpus_")
            try:
                work = os.path.join(tmp, "copy")
                os.makedirs(work)
                path = copy_with_sidecars(src, work)
                before = dir_snapshot(work)
                try:
                    s = Session.open(path, hash_evidence=False)
                except SessionError:
                    continue            # encrypted / not a database
                oracle_dir = os.path.join(tmp, "oracle")
                os.makedirs(oracle_dir)
                oracle = None
                try:
                    # No -journal for the oracle: plain SQLite would roll a hot journal back, while
                    # the tool deliberately shows the main file as found (and warns in a banner).
                    oracle = sqlite3.connect(copy_with_sidecars(src, oracle_dir,
                                                                sidecars=("-wal", "-shm")))
                    oracle.text_factory = lenient
                    register_collations(oracle, s.schema.collations)
                    tables_checked += self.compare_tables(src, s, oracle, failures)
                finally:
                    # Close both even when a table fails, or the temp dir cannot be removed on Windows
                    if oracle is not None:
                        oracle.close()
                    report = s.close()
                if not report.unchanged or dir_snapshot(work) != before:
                    failures.append("%s: evidence changed" % src)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        print("\ncorpus tables compared natively:", tables_checked)
        self.assertEqual(failures, [])

    def compare_tables(self, src, s, oracle, failures):
        """Compare every natively readable table with SQLite; returns the tables compared."""
        tables_checked = 0
        excluded = any(x in os.path.basename(src) for x in EXCLUDED)
        for name in s.tables():
            t = s.info(name)
            if t.kind != "table":
                continue
            page = s.browse(name, 0, 3)
            n = s.count(name)
            if n and not page.rows and not page.note:
                failures.append("%s %s: empty without explanation" % (src, name))
            if excluded or not t.natively_readable or not t.rowid_name and not t.without_rowid:
                continue
            cols = [c.name for c in t.columns if c.hidden not in (1, 2)]
            sel = ", ".join(quote_ident(c) for c in cols)
            try:
                want = oracle.execute("SELECT %s%s FROM %s" % (
                    "" if t.without_rowid else "_rowid_, ", sel, quote_ident(name))).fetchall()
            except sqlite3.Error:
                continue        # SQLite cannot read it: nothing to compare against
            got = []
            for row in s._native_table(name).iter_all():
                loc, values, _flags = row
                vals = [values[i] for i, c in enumerate(t.columns) if c.hidden not in (1, 2)]
                got.append(tuple(vals) if t.without_rowid else (loc.value,) + tuple(vals))
            tables_checked += 1
            if sorted(tuple(map(norm, r)) for r in want) != sorted(tuple(map(norm, r)) for r in got):
                failures.append("%s %s: native rows differ from SQLite" % (src, name))
        return tables_checked


if __name__ == "__main__":
    unittest.main()
