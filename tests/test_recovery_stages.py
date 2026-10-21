"""A recovery names its long steps with how far they are (engine Forensics.stage), cleared
when it ends; the Forensics tab's job line reads them with the time left and elapsed."""
import time
import unittest

import tests.helpers  # noqa: F401 - the src path
from tests.helpers import TempDirTest
from tests.fixtures import forensic_fixtures as ff


class StageTest(TempDirTest):
    def test_stages_reported_and_cleared(self):
        from engine.session import Session
        s = Session.open(ff.deleted_rows(self.tmp), hash_evidence=False)
        try:
            fx = s.forensics
            seen = []
            _set = fx.set_stage

            def spy(what, done=None, total=None):
                seen.append((what, done, total))
                _set(what, done, total)
            fx.set_stage = spy
            fx.carve()
            names = [w for w, _d, _t in seen]
            self.assertIn("Looking for dropped tables", names)
            self.assertIn("Reading the pages", names)
            self.assertTrue(any(n.startswith("Comparing with the live rows of ") for n in names))
            self.assertIsNone(fx.stage)           # cleared at the end
        finally:
            s.close()

    def test_job_text(self):
        from forensics_tab import job_text
        t = job_text("Recovering deleted records",
                     ("Reading the pages", 1234, 3922, time.time() - 20), 42)
        self.assertIn("Reading the pages: 1,234 of 3,922", t)
        self.assertIn("left in this step", t)
        self.assertTrue(t.endswith("0:42 elapsed"))
        t = job_text("Recovering deleted records", ("Looking for dropped tables", None, None,
                                                    time.time()), 3)
        self.assertNotIn("left", t)
        self.assertIn("Looking for dropped tables", t)


if __name__ == "__main__":
    unittest.main()
