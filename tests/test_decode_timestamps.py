"""engine.decode.timestamps: every epoch/unit against independently known offsets."""
import unittest
from datetime import datetime

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from engine.decode import timestamps as ts

WHEN = datetime(2021, 3, 4, 5, 6, 7)
UNIX = 1614834367                     # WHEN as Unix seconds
# Well-known distances between epochs, in seconds.
COCOA_OFFSET = 978307200              # 1970-01-01 -> 2001-01-01
NT_OFFSET = 11644473600               # 1601-01-01 -> 1970-01-01
HFS_OFFSET = 2082844800               # 1904-01-01 -> 1970-01-01
DOTNET_OFFSET = 62135596800           # 0001-01-01 -> 1970-01-01
GPS_OFFSET = 315964800                # 1970-01-01 -> 1980-01-06


class KnownDateTest(unittest.TestCase):
    CASES = {
        "unix_s": UNIX,
        "unix_ms": UNIX * 1000,
        "unix_us": UNIX * 10 ** 6,
        "unix_ns": UNIX * 10 ** 9,
        "cocoa_s": UNIX - COCOA_OFFSET,
        "cocoa_ns": (UNIX - COCOA_OFFSET) * 10 ** 9,
        "webkit_us": (UNIX + NT_OFFSET) * 10 ** 6,
        "filetime": (UNIX + NT_OFFSET) * 10 ** 7,
        "hfs_s": UNIX + HFS_OFFSET,
        "dotnet_ticks": (UNIX + DOTNET_OFFSET) * 10 ** 7,
        "gps_s": UNIX - GPS_OFFSET,
    }

    def test_every_integer_kind(self):
        for kind, value in self.CASES.items():
            self.assertEqual(ts.to_datetime(value, kind), WHEN, kind)
            self.assertEqual(ts.to_iso(value, kind), "2021-03-04T05:06:07Z", kind)

    def test_all_kinds_are_covered(self):
        self.assertEqual(set(self.CASES) | {"ole_days"}, set(k[0] for k in ts.KINDS))
        self.assertEqual(set(ts.LABELS), set(k[0] for k in ts.KINDS))

    def test_ole_days(self):
        days = 44259 + (5 * 3600 + 6 * 60 + 7) / 86400.0      # 2021-03-04 is day 44259
        got = ts.to_datetime(days, "ole_days")
        self.assertLess(abs((got - WHEN).total_seconds()), 0.001)
        self.assertEqual(ts.to_datetime(-1.25, "ole_days"), datetime(1899, 12, 29, 6, 0))

    def test_floats_and_fractions(self):
        self.assertEqual(ts.to_iso(UNIX + 0.25, "unix_s"), "2021-03-04T05:06:07.250000Z")
        self.assertEqual(ts.to_iso(float(UNIX - COCOA_OFFSET), "cocoa_s"),
                         "2021-03-04T05:06:07Z")
        self.assertEqual(ts.to_iso(-1, "unix_s"), "1969-12-31T23:59:59Z")

    def test_numeric_strings(self):
        self.assertEqual(ts.to_iso(" %d " % UNIX, "unix_s"), "2021-03-04T05:06:07Z")
        self.assertEqual(ts.to_iso("%d.5" % UNIX, "unix_s"), "2021-03-04T05:06:07.500000Z")
        self.assertEqual(ts.to_iso(b"1614834367", "unix_s"), "2021-03-04T05:06:07Z")

    def test_impossible_values_never_raise(self):
        for value in (None, True, False, "abc", "", "nan", "inf", float("nan"), float("inf"),
                      10 ** 30, -10 ** 30, 1e308, -1e308, [], {}, b"\xff", "9" * 5000):
            for kind, _label, _epoch, _unit in ts.KINDS:
                self.assertIsNone(ts.to_datetime(value, kind), (value, kind))
        self.assertIsNone(ts.to_iso(UNIX, "no_such_kind"))

    def test_old_years_format_everywhere(self):
        self.assertEqual(ts.to_iso(0, "dotnet_ticks"), "0001-01-01T00:00:00Z")
        self.assertEqual(ts.iso(datetime(9, 2, 3, 4, 5, 6, 7)), "0009-02-03T04:05:06.000007Z")


class GuessTest(unittest.TestCase):
    def test_unix_seconds_ranked_first(self):
        found = ts.guess(UNIX)
        self.assertEqual(found[0], ("unix_s", "2021-03-04T05:06:07Z"))
        self.assertTrue(all(1990 <= int(iso[:4]) <= 2040 for _k, iso in found))

    def test_cocoa_float_beats_1990s_unix(self):
        found = ts.guess(float(UNIX - COCOA_OFFSET) + 0.5)
        self.assertEqual(found[0][0], "cocoa_s")
        self.assertIn("unix_s", [k for k, _ in found])     # 1990: still in the window

    def test_big_units(self):
        self.assertEqual(ts.guess((UNIX + NT_OFFSET) * 10 ** 7)[0][0], "filetime")
        self.assertEqual(ts.guess((UNIX + NT_OFFSET) * 10 ** 6)[0][0], "webkit_us")
        self.assertEqual(ts.guess(UNIX * 1000)[0][0], "unix_ms")

    def test_values_near_an_epoch_are_not_guesses(self):
        self.assertEqual(ts.guess(5), [])
        self.assertEqual(ts.guess(0), [])

    def test_window_and_bad_input(self):
        narrow = ts.guess(UNIX, 2022, 2030)
        self.assertNotIn("unix_s", [k for k, _ in narrow])          # 2021 is outside
        self.assertTrue(all(2022 <= int(iso[:4]) <= 2030 for _k, iso in narrow))
        for bad in (None, "x", float("nan"), True, 10 ** 400):
            self.assertEqual(ts.guess(bad), [])
        self.assertEqual(ts.guess(UNIX, "a", "b"), [])

    def test_deterministic(self):
        self.assertEqual(ts.guess(123456789012), ts.guess(123456789012))


class ReadingsTest(unittest.TestCase):
    """readings(): the dates a row window shows beside a number (the one decoder)."""

    def test_unix_ms_keeps_utc_and_milliseconds(self):
        found = ts.readings(1600000000123)
        self.assertEqual(found[0], ("unix_ms", "Unix milliseconds",
                                    "2020-09-13 12:26:40.123 UTC"))
        self.assertTrue(all(when.endswith(" UTC") for _k, _l, when in found))

    def test_names_and_format_are_the_decoders(self):
        for kind, label, when in ts.readings(UNIX):
            self.assertEqual(label, ts.LABELS[kind])
            self.assertIn(kind, ts.READING_KINDS)
            self.assertRegex(when, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)? UTC$")
        self.assertEqual(ts.readings(UNIX)[0][2], "2021-03-04 05:06:07 UTC")

    def test_ids_counts_and_other_types_give_nothing(self):
        for v in (0, 5, 12345, 99999999, True, None, "1614834367", b"\x01", float("nan")):
            self.assertEqual(ts.readings(v), [], v)
        self.assertNotIn("gps_s", [k for k, _l, _w in ts.readings(UNIX)])

    def test_one_window_everywhere(self):
        from engine import timeline as tl
        self.assertEqual((tl.LO_YEAR, tl.HI_YEAR), (ts.LO_YEAR, ts.HI_YEAR))
        self.assertEqual(tl.SHORT["unix_ms"], ts.SHORT["unix_ms"])
        self.assertIs(tl.fmt_time, ts.fmt_time)


if __name__ == "__main__":
    unittest.main()
