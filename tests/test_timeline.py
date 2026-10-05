"""Timeline engine: the date-column detector (every kind, and columns that only look like dates),
events read in SQL and natively, date ranges, caps, WAL versions, recovered records, export."""
import csv
import json
import os
import unittest
from datetime import datetime, timedelta

from tests.helpers import TempDirTest, dir_snapshot
from tests.fixtures import timeline_fixtures as tf
from engine import timeline as tl
from engine.forensics.provenance import Provenance, Record
from engine.session import Session

NOW = datetime(2026, 9, 29, 12, 0, 0)


def dates(n=60, start=datetime(2019, 5, 6, 7, 8, 9), step=timedelta(days=17, seconds=3917)):
    """n dates that do not step evenly (as real ones do not)."""
    return [start + i * step + timedelta(seconds=(i * i * 131) % 5000) for i in range(n)]


def raw(kind, dts=None):
    return [tl.from_utc(d, kind) for d in (dts or dates())]


class NameTest(unittest.TestCase):
    def test_date_names(self):
        for name in ("timestamp", "sort_timestamp", "created", "created_at", "last_visit_time",
                     "expires_utc", "ZCREATIONDATE", "ZDATE", "lastModified", "mtime", "sent",
                     "received_ts", "join_ts", "visit_time", "birthday"):
            self.assertEqual(tl.name_hint(name), "strong", name)
        self.assertEqual(tl.name_hint("x", "DATETIME"), "strong")
        self.assertEqual(tl.name_hint("call_start"), "weak")

    def test_names_that_are_not_dates(self):
        for name in ("_id", "message_row_id", "phone", "phone_number", "media_size",
                     "item_count", "duration", "timeout_ms", "time_zone", "is_deleted",
                     "has_date", "timestamp_id", "key_remote_jid", "file_name", "lat", "flags"):
            self.assertEqual(tl.name_hint(name), "never", name)
        for name in ("value", "v", "data", "contact"):
            self.assertIsNone(tl.name_hint(name), name)


class JudgeTest(unittest.TestCase):
    def check(self, name, values, kind, decl=""):
        g = tl.judge(name, values, decl, now=NOW)
        self.assertEqual(g.kind, kind, "%s: %s" % (name, g.reason))
        return g

    def test_every_kind_by_name(self):
        for kind in ("unix_s", "unix_ms", "unix_us", "unix_ns", "cocoa_s", "cocoa_ns",
                     "webkit_us", "filetime", "hfs_s", "dotnet_ticks"):
            dts = dates()
            if kind == "cocoa_ns":      # clear of the .NET ticks overlap (see test_net_or_cocoa)
                dts = dates(start=datetime(2022, 6, 1))
            g = self.check("created", raw(kind, dts), kind)
            self.assertEqual(g.confidence, "high")
            self.assertEqual((g.first, g.last), (min(dts), max(dts)), kind)
        self.check("modified", [float(v) for v in raw("ole_days")], "ole_days")
        self.check("created", [float(v) + 0.25 for v in raw("cocoa_s")], "cocoa_s")

    def test_every_numeric_kind_by_values_alone(self):
        for kind in ("unix_s", "unix_ms", "unix_us", "unix_ns", "webkit_us", "filetime",
                     "dotnet_ticks", "cocoa_s"):
            dts = dates(start=datetime(2023, 1, 1)) if kind == "cocoa_s" else dates()
            g = self.check("v", raw(kind, dts), kind)
            self.assertIn(g.confidence, ("medium", "low"))
            self.assertIn("values only", g.reason)

    def test_date_text(self):
        iso = [d.strftime("%Y-%m-%dT%H:%M:%S.%fZ") for d in dates()]
        self.check("v", iso, tl.ISO)
        spaced = [d.strftime("%Y-%m-%d %H:%M:%S") for d in dates()]
        self.check("created", spaced, tl.ISO)
        rfc = [d.strftime("%a, %d %b %Y %H:%M:%S GMT") for d in dates()]
        g = self.check("last_modified", rfc + [""] * 50, tl.ISO)
        self.assertTrue(g.loose_text)
        self.assertEqual(g.unset, 50)
        self.assertEqual(tl.parse_iso("2021-03-04T10:00:00+02:00"), datetime(2021, 3, 4, 8))
        self.assertEqual(tl.parse_iso("2021-03-04 10:00-0130"), datetime(2021, 3, 4, 11, 30))
        self.assertEqual(tl.parse_rfc("Thu, 04 Mar 2021 10:00:00 +0100"), datetime(2021, 3, 4, 9))
        self.assertIsNone(tl.parse_iso("2021-13-40"))

    def test_numeric_text_needs_a_date_name(self):
        text = [str(v) for v in raw("unix_ms")]
        g = self.check("sent_time", text, "unix_ms")
        self.assertTrue(g.text_numbers)
        self.check("v", text, None)

    def test_unset_values_do_not_count(self):
        g = self.check("expires", raw("webkit_us") + [0] * 200 + [-1], "webkit_us")
        self.assertEqual(g.unset, 201)
        self.check("expires", [0, 0, None, -1], None)

    def test_traps(self):
        ids = list(range(1, 501))
        self.check("v", ids, None)                                      # row ids
        self.check("n", [50000000 + i for i in range(500)], None)       # a counter far from 0
        self.check("n", [1400000000 + 7 * i for i in range(500)], None) # an evenly stepping id
        self.check("contact", ["+91 98765 %05d" % i for i in range(60)], None)
        self.check("contact", ["919876%06d" % i for i in range(60)], None)  # numbers as text
        self.check("phone", [919876543210 + i for i in range(60)], None)    # named: never
        self.check("mobile", [9876543210 + 1111 * i for i in range(60)], None)
        sizes = [(i * 7919 * 104729) % 4000000000 for i in range(60)]
        self.check("file_size", sizes, None)
        self.check("used", [10 ** (i % 10) + i for i in range(60)], None)
        self.check("v", [40000.5 + i for i in range(60)], None)         # OLE needs a date name
        self.check("v", [i % 2 for i in range(60)], None)               # booleans
        self.check("transition", [805306368, 805306376, 268435457, 838860808] * 15, None)
        self.check("amount", [12.5 + i for i in range(60)], None)
        self.check("date", [float(i) for i in range(1, 1000)], None)    # Cocoa near 2001-01-01
        self.check("v", [b"\x00\x01"] * 20, None)                       # BLOBs

    def test_gps_is_never_guessed(self):
        gps = raw("gps_s")
        g = self.check("gps_time", gps, "unix_s")
        self.assertNotIn("gps_s", g.alternatives)
        self.assertEqual(tl.to_utc(gps[0], "gps_s"), dates()[0])

    def test_ambiguous_seconds(self):
        # 2026 Cocoa seconds are also 1995 Unix seconds: the reading nearer to now wins
        self.check("created", raw("cocoa_s", dates(start=datetime(2026, 1, 5), n=20,
                                                   step=timedelta(days=5))), "cocoa_s")
        # 2001 Unix seconds are also 2032 Cocoa seconds: dates far in the future lose
        self.check("created", raw("unix_s", dates(start=datetime(2001, 1, 5), n=20,
                                                  step=timedelta(days=5))), "unix_s")

    def test_net_or_cocoa(self):
        # Cocoa ns of early 2021 are .NET ticks of any year: .NET wins, Cocoa is the alternative
        g = self.check("created", raw("dotnet_ticks"), "dotnet_ticks")
        self.assertIn("cocoa_ns", g.alternatives)

    def test_suggest_kind(self):
        self.assertEqual(tl.suggest_kind("ts", raw("unix_ms")), "unix_ms")
        self.assertIsNone(tl.suggest_kind("ts", ["abc"]))


class ConvertTest(unittest.TestCase):
    def test_round_trip(self):
        d = datetime(2022, 2, 3, 4, 5, 6, 789000)
        for kind in tl.NUMERIC_KINDS:
            self.assertEqual(tl.to_utc(tl.from_utc(d, kind), kind), d, kind)
        self.assertEqual(tl.from_utc(d, tl.ISO), "2022-02-03 04:05:06")

    def test_format_and_formatter(self):
        self.assertEqual(tl.fmt_time(datetime(2021, 1, 2, 3, 4, 5)), "2021-01-02 03:04:05")
        self.assertEqual(tl.fmt_time(datetime(2021, 1, 2, 3, 4, 5, 120000)),
                         "2021-01-02 03:04:05.120")
        self.assertEqual(tl.fmt_time(datetime(2021, 1, 2, 3, 4, 5, 7)),
                         "2021-01-02 03:04:05.000007")
        f = tl.formatter("unix_ms")
        self.assertEqual(f(1614834367000), "2021-03-04 05:06:07")
        self.assertIsNone(f("text"))
        self.assertIsNone(f(b"\x01"))
        self.assertIsNone(tl.formatter("webkit_us")(None))

    def test_offsets_and_dates(self):
        self.assertEqual(tl.parse_offset("UTC+05:30"), 330)
        self.assertEqual(tl.parse_offset("-8"), -480)
        self.assertEqual(tl.parse_offset("utc"), 0)
        self.assertIsNone(tl.parse_offset("UTC+20"))
        self.assertIsNone(tl.parse_offset("later"))
        self.assertEqual(tl.offset_label(-210), "UTC-03:30")
        self.assertEqual(tl.parse_when("2021-03-04"), datetime(2021, 3, 4))
        self.assertEqual(tl.parse_when("2021-03-04", end=True),
                         datetime(2021, 3, 4, 23, 59, 59, 999999))
        self.assertEqual(tl.parse_when("2021-03-04 10:11", end=True),
                         datetime(2021, 3, 4, 10, 11, 59, 999999))
        self.assertIsNone(tl.parse_when(" "))
        with self.assertRaises(ValueError):
            tl.parse_when("4 March")


class DatabaseTest(TempDirTest):
    wal = True

    def setUp(self):
        TempDirTest.setUp(self)
        self.path = tf.build(self.tmp, wal=self.wal)
        self.before = dir_snapshot(self.tmp)
        self.s = Session.open(self.path, hash_evidence=False)
        self.det = tl.detect(self.s, now=NOW)

    def tearDown(self):
        self.s.close()
        self.assertEqual(dir_snapshot(self.tmp), self.before, "the evidence folder changed")
        TempDirTest.tearDown(self)

    def events_of(self, events, table, column):
        return sorted((e for e in events if (e.table, e.column) == (table, column)),
                      key=lambda e: (e.when, e.row))

    def key(self, e):
        return (e.table, e.column, e.row, e.when, repr(e.raw), e.description)


class DetectTest(DatabaseTest):
    def test_kinds_and_traps(self):
        for (table, column), kind in tf.EXPECTED.items():
            c = self.det.get(table, column)
            self.assertIsNotNone(c, "%s.%s not detected" % (table, column))
            self.assertEqual(c.kind, kind, "%s.%s: %s" % (table, column, c.reason))
            self.assertTrue(c.reason)
        for table, column in tf.TRAPS:
            c = self.det.get(table, column)
            self.assertTrue(c is None or c.kind is None, "%s.%s taken for a date: %s"
                            % (table, column, c.reason if c else ""))
        self.assertEqual(self.det.get("plain", "v").confidence, "medium")
        self.assertNotIn("traps", set(c.table for c in self.det.detected()))

    def test_descriptions(self):
        d = self.det.descriptions
        self.assertIn("data", d["messages"])
        self.assertNotIn("_id", d["messages"])           # the row id is the Row column
        self.assertNotIn("timestamp", d["messages"])
        self.assertEqual(d["urls"][:2], ["url", "title"])

    def test_overrides(self):
        det = tl.detect(self.s, overrides={"sheet": {"gps_time": "gps_s"},
                                           "messages": {"send_time": tl.OFF, "data": "bogus"}})
        self.assertEqual(det.get("sheet", "gps_time").effective_kind, "gps_s")
        self.assertFalse(det.get("messages", "send_time").enabled)
        self.assertEqual(det.get("messages", "timestamp").effective_kind, "unix_ms")
        ev = tl.build_events(self.s, [det.get("sheet", "gps_time")], det.descriptions)
        self.assertEqual(ev.events[0].when, tf.when(0))

    def test_cancel(self):
        det = tl.detect(self.s, cancel=lambda: True)
        self.assertTrue(det.cancelled)
        self.assertEqual(det.columns, [])


class EventsTest(DatabaseTest):
    def test_events_match_the_rows(self):
        res = tl.build_events(self.s, self.det.columns, self.det.descriptions)
        self.assertFalse(res.cancelled)
        self.assertEqual(res.events, sorted(res.events, key=lambda e: e.when))
        visits = self.events_of(res.events, "urls", "last_visit_time")
        self.assertEqual(len(visits), tf.ROWS - tf.ROWS // 10)      # unset (0) rows have none
        self.assertEqual([e.when for e in visits],
                         [tf.when(i) for i in range(tf.ROWS) if i % 10 != 9])
        first = visits[0]
        self.assertEqual((first.row, first.source, first.kind), ("1", "DB", "webkit_us"))
        self.assertIn("url=https://example.org/0", first.description)
        notes = self.events_of(res.events, "notes", "modified")
        self.assertEqual([e.when for e in notes], [tf.when(i) for i in range(tf.ROWS)])
        self.assertEqual(notes[0].locator.kind, "pk")
        msgs = self.events_of(res.events, "messages", "timestamp")
        expect = tf.ROWS - 1 if self.wal else tf.ROWS                  # row 7 deleted in the WAL
        self.assertEqual(len(msgs), expect)

    def test_sql_and_native_agree(self):
        if self.s.source("messages") != "sql":
            self.skipTest("SQLite cannot see the WAL in %s mode: no SQL to compare"
                          % self.s.mode)
        for c in self.det.enabled():
            desc = [d for d in self.det.descriptions.get(c.table, ()) if d != c.column]
            sql, _ = tl._sql_column_events(self.s, c, c.effective_kind, desc, None, None, 1000,
                                           None)
            self.s.source = lambda name: "native"       # the native reader, not SQLite
            try:
                nat, _ = tl._native_column_events(self.s, c, c.effective_kind, desc, None,
                                                  None, 1000, None)
            finally:
                del self.s.source
            self.assertEqual(sorted(map(self.key, sql)), sorted(map(self.key, nat)),
                             "%s.%s" % (c.table, c.column))
            self.assertTrue(sql, "%s.%s has no events" % (c.table, c.column))

    def test_native_session(self):
        want = tl.build_events(self.s, self.det.columns, self.det.descriptions)
        self.s.source = lambda name: "native"           # as for a file SQLite cannot read
        got = tl.build_events(self.s, self.det.columns, self.det.descriptions)
        self.assertEqual([self.key(e) for e in got.events], [self.key(e) for e in want.events])

    def test_date_range(self):
        full = tl.build_events(self.s, self.det.columns, self.det.descriptions).events
        start, end = datetime(2021, 3, 5, 12), datetime(2021, 3, 7, 23, 59, 59)
        for native in (False, True):
            if native:
                self.s.source = lambda name: "native"
            got = tl.build_events(self.s, self.det.columns, self.det.descriptions, start, end)
            want = [e for e in full if start <= e.when <= end]
            self.assertTrue(want)
            self.assertEqual([self.key(e) for e in got.events], [self.key(e) for e in want])

    def test_cap_keeps_the_newest(self):
        full = tl.build_events(self.s, self.det.columns, self.det.descriptions).events
        res = tl.build_events(self.s, self.det.columns, self.det.descriptions, column_cap=10)
        for c in self.det.enabled():
            got = self.events_of(res.events, c.table, c.column)
            want = self.events_of(full, c.table, c.column)[-10:]
            self.assertEqual([self.key(e) for e in got], [self.key(e) for e in want])
        self.assertIn(("messages", "timestamp"), res.capped)
        self.assertTrue(any("newest 10" in n for n in res.notes))
        few = tl.build_events(self.s, self.det.columns, self.det.descriptions, total_cap=25)
        self.assertEqual(len(few.events), 25)
        self.assertEqual(few.events[-1].when, full[-1].when)

    def test_loose_text_dates(self):
        c = self.det.get("notes", "created")
        c.loose_text = True                             # read like RFC 2822 text: no SQL range
        got = tl.build_events(self.s, [c], self.det.descriptions,
                              datetime(2021, 3, 5), datetime(2021, 3, 6))
        self.assertTrue(got.events)
        self.assertTrue(all(datetime(2021, 3, 5) <= e.when <= datetime(2021, 3, 6)
                            for e in got.events))

    def test_cancel(self):
        res = tl.build_events(self.s, self.det.columns, self.det.descriptions,
                              cancel=lambda: True)
        self.assertTrue(res.cancelled)

    def test_wal_versions(self):
        if not self.wal:
            return
        from wal_parser import WALParser

        def live(table, loc):
            row = self.s.row(table, loc)
            return row.values if row is not None else None
        evs, _notes = tl.wal_events(WALParser(self.s).recover_all_records(), self.det.columns,
                                    self.det.descriptions, live_values=live)
        ts = [e for e in evs if e.column == "timestamp"]
        self.assertEqual(sorted(e.locator.value for e in ts), [5, 7])
        self.assertTrue(all(e.source.startswith("WAL (") for e in ts))
        five = [e for e in ts if e.locator.value == 5][0]
        self.assertEqual(five.when, tf.when(4) + timedelta(seconds=1))   # the first edit
        self.assertIn("edited once", five.description)
        seven = [e for e in ts if e.locator.value == 7][0]
        self.assertEqual(seven.when, tf.when(6))                      # the deleted row
        ranged, _ = tl.wal_events(WALParser(self.s).recover_all_records(), self.det.columns,
                                  self.det.descriptions, start=tf.when(5), live_values=live)
        self.assertEqual(sorted(e.locator.value for e in ranged if e.column == "timestamp"),
                         [7])

    def test_recovered_records(self):
        prov = Provenance("freeblock", "main", 3, 100)
        cols = ["_id", "key_remote_jid", "data", "timestamp", "send_time", "phone", "media_size",
                "counter", "lat"]
        stamp = tl.from_utc(datetime(2020, 1, 2, 3, 4, 5), "unix_ms")
        recs = [Record("messages", cols, [99, "x@s", "gone", stamp, None, 1, 2, 3, 4.0], prov,
                       "high", rowid=99),
                Record("urls", ["id", "url", "title", "last_visit_time"], [1, "u", "t", 0],
                       prov, "low", rowid=None),
                Record(None, ["a"], [stamp], prov)]
        evs, _ = tl.carved_events(recs, self.det.columns, self.det.descriptions)
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e.table, e.column, e.when), ("messages", "timestamp",
                                                        datetime(2020, 1, 2, 3, 4, 5)))
        self.assertTrue(e.source.startswith("Recovered (freeblock"))
        self.assertEqual(e.locator.value, 99)
        self.assertIs(e.ref, recs[0])
        self.assertIn("data=gone", e.description)

    def test_export(self):
        res = tl.build_events(self.s, self.det.columns, self.det.descriptions)
        out = os.path.join(self.tmp, "..", os.path.basename(self.tmp) + "_out")
        os.makedirs(out)
        try:
            info = {"database": self.path}
            for fmt in ("csv", "json", "html"):
                p = os.path.join(out, "t." + fmt)
                n = tl.export_events(p, fmt, res.events, info, offset_minutes=330)
                self.assertEqual(n, len(res.events))
            with open(os.path.join(out, "t.csv"), encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][:3], ["time_utc", "time_local", "table"])
            self.assertEqual(len(rows) - 1, len(res.events))
            with open(os.path.join(out, "t.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["format"], tl.FILE_FORMAT)
            self.assertEqual(len(data["events"]), len(res.events))
            self.assertEqual(data["info"]["local_offset"], "UTC+05:30")
            with open(os.path.join(out, "t.html"), encoding="utf-8") as f:
                self.assertIn("%d events" % len(res.events), f.read())
            with self.assertRaises(ValueError):
                tl.export_events(os.path.join(self.tmp, "t.csv"), "csv", res.events,
                                 is_protected=lambda p: True)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "t.csv")))
        finally:
            import shutil
            shutil.rmtree(out, ignore_errors=True)


class NoWalEventsTest(EventsTest):
    wal = False


if __name__ == "__main__":
    unittest.main()
