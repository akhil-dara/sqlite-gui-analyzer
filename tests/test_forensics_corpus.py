"""Forensics over a whole corpus. Set SQLITE_CORPUS to a folder of SQLite test databases.

Each database is copied to a temp folder first (test data, not evidence), then carve,
dropped-schema recovery, audit and the journal reader run with a time cap; none may raise,
and the copy's folder must be byte-identical after the session closes.
"""
import os
import shutil
import tempfile
import traceback
import unittest

from tests.helpers import copy_with_sidecars, dir_snapshot
from tests.test_corpus import CORPUS, corpus_files
from engine.session import Session, SessionError

TIME_CAP = float(os.environ.get("SQLITE_CORPUS_CAP", "30"))


@unittest.skipUnless(CORPUS, "set SQLITE_CORPUS to run corpus tests")
class ForensicsCorpusTest(unittest.TestCase):
    def test_forensics_never_raise(self):
        failures, records, databases = [], 0, 0
        for src in sorted(corpus_files()):
            tmp = tempfile.mkdtemp(prefix="sga_fxcorpus_")
            try:
                path = copy_with_sidecars(src, tmp)
                before = dir_snapshot(tmp)
                try:
                    s = Session.open(path, hash_evidence=False)
                except SessionError:
                    continue
                databases += 1
                try:
                    fx = s.forensics
                    fx.dropped_schema(time_limit=TIME_CAP / 2)
                    res = fx.carve(time_limit=TIME_CAP)
                    records += len(res)
                    fx.audit(time_limit=TIME_CAP / 2)
                    fx.journal()
                except Exception:
                    failures.append("%s: %s" % (src, traceback.format_exc()))
                finally:
                    if not s.close().unchanged or dir_snapshot(tmp) != before:
                        failures.append("%s: evidence changed" % src)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        print("\nforensics corpus: %d databases, %d records" % (databases, records))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
