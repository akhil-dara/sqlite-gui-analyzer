import struct
import unittest

from tests import helpers  # noqa: F401  (sets sys.path)
from engine.fileformat.record import (InvalidText, RecordError, decode_record,
                                      decode_record_lenient, parse_header, read_varint,
                                      serial_size, to_signed64)
from engine.issues import IssueLog


def varint(v):
    """Encode an unsigned 64-bit value as an SQLite varint (test helper)."""
    if v > 0x00FFFFFFFFFFFFFF:
        out = [v & 0xFF]
        v >>= 8
        for _ in range(8):
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))
    parts = [v & 0x7F]
    v >>= 7
    while v:
        parts.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(parts))


def record(*cells):
    """Build a record from (serial_type, body_bytes) pairs."""
    header = b"".join(varint(st) for st, _ in cells)
    hlen = len(header) + 1
    if hlen > 127:
        hlen += 1
    return varint(hlen) + header + b"".join(body for _, body in cells)


class VarintTest(unittest.TestCase):
    def test_round_trips(self):
        for v in (0, 1, 127, 128, 16383, 16384, 2 ** 32, 2 ** 56 - 1, 2 ** 56, 2 ** 64 - 1):
            enc = varint(v)
            self.assertEqual(read_varint(enc, 0), (v, len(enc)), v)

    def test_nine_byte_form_uses_all_eight_bits_of_last_byte(self):
        self.assertEqual(read_varint(b"\xff" * 9, 0), (2 ** 64 - 1, 9))

    def test_truncated_raises(self):
        with self.assertRaises(RecordError):
            read_varint(b"\x81\x82", 0)

    def test_signed_rowid(self):
        self.assertEqual(to_signed64(2 ** 64 - 1), -1)
        self.assertEqual(to_signed64(5), 5)


class SerialTypeTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual([serial_size(t) for t in range(12)], [0, 1, 2, 3, 4, 6, 8, 8, 0, 0, 0, 0])
        self.assertEqual(serial_size(12), 0)
        self.assertEqual(serial_size(13), 0)
        self.assertEqual(serial_size(20), 4)   # blob of 4
        self.assertEqual(serial_size(21), 4)   # text of 4


class DecodeRecordTest(unittest.TestCase):
    def test_every_storage_class(self):
        rec = record((0, b""), (1, b"\xff"), (2, b"\x01\x00"), (3, b"\x00\x01\x00"),
                     (4, struct.pack(">i", -70000)), (5, (2 ** 40).to_bytes(6, "big")),
                     (6, struct.pack(">q", -(2 ** 62))), (7, struct.pack(">d", 1.5)),
                     (8, b""), (9, b""), (12 + 2 * 3, b"\x00\x01\x02"), (13 + 2 * 5, b"hello"))
        self.assertEqual(decode_record(rec),
                         [None, -1, 256, 256, -70000, 2 ** 40, -(2 ** 62), 1.5, 0, 1,
                          b"\x00\x01\x02", "hello"])

    def test_utf16_text(self):
        body = "Ünï".encode("utf-16-le")
        rec = record((13 + 2 * len(body), body))
        self.assertEqual(decode_record(rec, "utf-16-le"), ["Ünï"])

    def test_invalid_text_is_kept_as_bytes_and_logged(self):
        issues = IssueLog()
        rec = record((13 + 2 * 3, b"\xff\xfeA"))
        value = decode_record(rec, "utf-8", issues)[0]
        self.assertIsInstance(value, InvalidText)
        self.assertEqual(bytes(value), b"\xff\xfeA")
        self.assertEqual([i.kind for i in issues], ["invalid_text"])

    def test_reserved_serial_types_decode_as_null_with_issue(self):
        issues = IssueLog()
        self.assertEqual(decode_record(record((10, b"")), "utf-8", issues), [None])
        self.assertEqual(issues.items[0].kind, "reserved_serial")

    def test_strict_decode_rejects_truncated_body(self):
        with self.assertRaises(RecordError):
            decode_record(record((13 + 2 * 5, b"hel")))

    def test_parse_header(self):
        rec = record((1, b"\x05"), (13 + 2 * 2, b"hi"), (0, b""))
        self.assertEqual(parse_header(rec), ([1, 17, 0], 4))
        for bad in (b"\x00", b"\x05\x01", b"\x03\x81\x81"):   # length 0, past the end, overrun
            with self.assertRaises(RecordError):
                parse_header(bad)

    def test_header_length_over_127_takes_a_two_byte_varint(self):
        # 150 integer columns: the header is 151+ bytes, so its length is a 2-byte varint
        cells = [(1, bytes([i % 100])) for i in range(150)]
        rec = record(*cells)
        self.assertEqual(rec[0] & 0x80, 0x80)
        types, body = parse_header(rec)
        self.assertEqual((len(types), body), (150, 152))
        self.assertEqual(decode_record(rec), [i % 100 for i in range(150)])
        self.assertEqual(decode_record_lenient(rec), ([i % 100 for i in range(150)], None))


class LenientDecodeTest(unittest.TestCase):
    def test_zero_header_length_gives_empty_row(self):
        self.assertEqual(decode_record_lenient(b"\x00abc"), ([], "record header length is 0"))

    def test_truncated_body_keeps_leading_columns(self):
        values, problem = decode_record_lenient(record((1, b"\x07"), (13 + 2 * 5, b"hel")))
        self.assertEqual(values, [7, "hel"])        # the bytes that exist of the cut column
        self.assertIn("truncated", problem)
        self.assertIn("3 of 5 bytes", problem)
        values, problem = decode_record_lenient(record((1, b"\x07"), (12 + 2 * 4, b"\x01\x02")))
        self.assertEqual(values, [7, b"\x01\x02"])
        values, problem = decode_record_lenient(record((1, b"\x07"), (4, b"\x00\x01")))
        self.assertEqual(values, [7])               # a cut number is not guessed

    def test_header_longer_than_payload(self):
        values, problem = decode_record_lenient(b"\x10\x01\x07")
        self.assertIn("exceeds payload", problem)

    def test_well_formed_record_has_no_problem(self):
        self.assertEqual(decode_record_lenient(record((1, b"\x05"))), ([5], None))

    def test_every_damaged_record_is_logged(self):
        for payload in (b"\x00abc", b"\x81", b"\x10\x01\x07"):     # length 0, bad varint, overrun
            issues = IssueLog()
            _values, problem = decode_record_lenient(payload, "utf-8", issues, "t row 3")
            self.assertEqual([(i.kind, i.detail, i.where) for i in issues],
                             [("damaged_record", problem, "t row 3")], payload)


class IssueLogTest(unittest.TestCase):
    def test_repeats_are_kept_but_counted_once(self):
        log = IssueLog()
        for _ in range(3):              # the same damaged row read by browse, search, detail
            log.add("damaged_record", "truncated", "t row 3")
        log.add("damaged_record", "truncated", "t row 4")
        log.add("damaged_record", "truncated", "t row 3", "error")
        self.assertEqual((len(log), log.distinct_count()), (5, 3))

    def test_cap(self):
        log = IssueLog(cap=2)
        for i in range(5):
            log.add("k", str(i))
        self.assertEqual((len(log), log.dropped, log.distinct_count()), (2, 3, 2))


if __name__ == "__main__":
    unittest.main()
