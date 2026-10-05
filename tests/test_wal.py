import unittest

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.fileformat.wal import (CURRENT, STALE, SUPERSEDED, UNCOMMITTED, WalError, WalFile,
                                   wal_checksum)


class ChecksumTest(unittest.TestCase):
    def test_known_vector(self):
        # two words 1 and 2: s0 = 0 + 1 + 0 = 1; s1 = 0 + 2 + 1 = 3
        self.assertEqual(wal_checksum(b"\x00\x00\x00\x01\x00\x00\x00\x02", 0, 0, True), (1, 3))
        self.assertEqual(wal_checksum(b"\x01\x00\x00\x00\x02\x00\x00\x00", 0, 0, False), (1, 3))

    def test_wraps_at_32_bits(self):
        s0, s1 = wal_checksum(b"\xff" * 8, 0xFFFFFFFF, 0xFFFFFFFF, True)
        self.assertTrue(0 <= s0 < 2 ** 32 and 0 <= s1 < 2 ** 32)


class WalStatesTest(TempDirTest):
    def open(self, builder):
        path = builder(self.tmp)
        w = WalFile(path + "-wal")
        self.addCleanup(w.close)
        return w

    def test_all_four_states_present(self):
        w = self.open(fx.wal_states)
        counts = w.state_counts()
        for state in (CURRENT, SUPERSEDED, UNCOMMITTED, STALE):
            self.assertGreater(counts[state], 0, (state, counts))
        self.assertTrue(w.header.checksum_ok)

    def test_committed_frames_before_last_commit_are_not_uncommitted(self):
        """Regression: the old parser marked every non-commit frame of a transaction 'uncommitted'."""
        w = self.open(fx.wal_states)
        for fr in w.frames[:w.last_commit + 1]:
            self.assertIn(fr.state, (CURRENT, SUPERSEDED))
            self.assertTrue(fr.checksum_ok)
            self.assertIsNotNone(fr.commit_group)

    def test_overlay_holds_latest_committed_frame_per_page(self):
        w = self.open(fx.wal_states)
        latest = {}
        for fr in w.frames[:w.last_commit + 1]:
            latest[fr.page_no] = fr.index
        self.assertEqual(w.overlay, latest)
        self.assertEqual(w.db_size_pages, w.frames[w.last_commit].db_size)

    def test_stale_frames_come_from_older_generation(self):
        w = self.open(fx.wal_states)
        stale = [f for f in w.frames if f.state == STALE]
        self.assertTrue(stale and all(not f.salt_match for f in stale))

    def test_uncommitted_frames_may_fail_checksum(self):
        """SQLite re-checksums overwritten frames only at commit: an open transaction's frames
        keep current salts but can break the chain. They are still 'uncommitted', not 'stale'."""
        w = self.open(fx.wal_states)
        unc = [f for f in w.frames if f.state == UNCOMMITTED]
        self.assertTrue(unc and all(f.salt_match for f in unc))
        self.assertTrue(all(f.index > w.last_commit for f in unc))

    def test_big_endian_magic_parses_to_same_states(self):
        le = [f.state for f in self.open(fx.wal_states).frames]
        be = self.open(fx.big_endian_wal)
        self.assertTrue(be.header.big_endian_words)
        self.assertEqual([f.state for f in be.frames], le)

    def test_rejects_non_wal(self):
        p = fx.freelist(self.tmp)
        with self.assertRaises(WalError):
            WalFile(p)


if __name__ == "__main__":
    unittest.main()
