"""A short, seeded run of tools/fuzz.py on every target: the parsers and decoders must not raise
what their API does not allow, nor take long, on mutated input. (tools/fuzz.py runs the same
targets for as long as asked, in child processes with a memory cap.)"""

import os
import sys
import time
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import fuzz  # noqa: E402

BLOB_INPUTS = 400
DB_INPUTS = 60
STRESS = os.environ.get("SGA_STRESS") == "1"


class QuickFuzzTest(unittest.TestCase):
    def run_target(self, target, count):
        t0 = time.time()
        n, found = fuzz.run_inputs(target, seed=20261001, count=count, slow=2.0, slow_db=25.0)
        self.assertEqual(n, count)
        self.assertEqual([(f.kind, f.info[-500:]) for f in found], [], target)
        return time.time() - t0

    def test_blob_targets(self):
        for target in fuzz.BLOB_TARGETS:
            with self.subTest(target=target):
                self.run_target(target, BLOB_INPUTS * (10 if STRESS else 1))

    def test_database_targets(self):
        for target in fuzz.DB_TARGETS:
            with self.subTest(target=target):
                self.run_target(target, DB_INPUTS * (10 if STRESS else 1))

    def test_the_same_seed_gives_the_same_inputs(self):
        import random
        seeds = fuzz.blob_seeds()
        a = [fuzz.mutate(random.Random(5), s) for s in seeds]
        b = [fuzz.mutate(random.Random(5), s) for s in seeds]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
