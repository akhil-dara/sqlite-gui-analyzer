"""Schemaless protobuf parsing: values, alternatives, strictness and limits."""
import time
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests import decode_samples as S
from tests.decode_samples import key, pb_bytes, pb_group, pb_varint, varint
from engine.decode import decode_blob, decoded_strings, interpretations, summary
from engine.decode.core import Context
from engine.decode import protobuf


def parse(data):
    return protobuf.decode(data, Context(), 0)


class ValuesTest(unittest.TestCase):
    def setUp(self):
        attempt = parse(S.PROTOBUF)
        self.assertEqual(attempt[1], "confident")
        self.node = attempt[2]
        self.fields = dict((c.label, c) for c in self.node.children)

    def test_shape_and_offsets(self):
        self.assertEqual(self.node.kind, "protobuf")
        self.assertEqual([c.label for c in self.node.children], [1, 2, 3, 4, 5, 6, 7])
        first = self.fields[1]
        self.assertEqual((first.kind, first.value, first.offset, first.length),
                         ("string", "hello world", 0, 13))
        # Every field spans tag..end of value, contiguously, except bytes (the payload).
        self.assertEqual(self.fields[2].offset, 13)

    def test_varints(self):
        self.assertEqual((self.fields[2].kind, self.fields[2].value), ("int", 150))
        self.assertIn("sint64 75", self.fields[2].note)
        self.assertEqual(self.fields[7].value, -2)
        self.assertIn("uint64 18446744073709551614", self.fields[7].note)

    def test_nested_message(self):
        nested = self.fields[3]
        self.assertEqual(nested.kind, "protobuf")
        inner = dict((c.label, c) for c in nested.children)
        self.assertEqual(inner[1].value, "nested text")
        self.assertEqual(inner[2].value, 5)
        self.assertIn("sint64 -3", inner[2].note)

    def test_fixed_width(self):
        self.assertEqual((self.fields[4].kind, self.fields[4].value), ("float", 3.5))
        self.assertIn("int64", self.fields[4].note)
        self.assertEqual((self.fields[6].kind, self.fields[6].value), ("float", 1.25))

    def test_packed_alternative(self):
        blob = self.fields[5]
        self.assertEqual(blob.kind, "bytes")
        payload = varint(1) + varint(300) + varint(70000)
        self.assertEqual(blob.value, payload)
        self.assertEqual(S.PROTOBUF[blob.offset:blob.offset + blob.length], payload)
        packed = [c for c in blob.children if c.label == "packed varints"]
        self.assertEqual([n.value for n in packed[0].children], [1, 300, 70000])
        self.assertEqual(packed[0].confidence, "uncertain")

    def test_public_api(self):
        self.assertEqual(summary(S.PROTOBUF), "protobuf (7 fields)")
        strings = decoded_strings(S.PROTOBUF)
        self.assertIn("hello world", strings)
        self.assertIn("nested text", strings)
        self.assertEqual(decode_blob(S.PROTOBUF).to_plain()["1"], "hello world")

    def test_groups(self):
        data = pb_group(2, pb_varint(1, 7) + pb_bytes(3, b"in group")) + pb_varint(4, 1)
        node = parse(data)[2]
        group = node.children[0]
        self.assertEqual((group.kind, group.label, group.note), ("protobuf", 2, "group"))
        self.assertEqual([c.value for c in group.children], [7, "in group"])


class StrictnessTest(unittest.TestCase):
    def failed(self, data):
        attempt = parse(data)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt[1], "failed", data)
        return attempt[3]

    def test_invalid_inputs(self):
        self.assertIn("field number 0", self.failed(b"\x00\x01"))
        self.assertIn("19000", self.failed(pb_varint(19000, 1) + pb_varint(1, 1)))
        self.assertIn("wire type 6", self.failed(b"\x0e\x01"))
        self.assertIn("non-canonical", self.failed(b"\x08\x81\x00"))
        self.assertIn("truncated", self.failed(b"\x08\x81"))
        self.assertIn("runs past", self.failed(b"\x0a\x05abc"))
        self.assertIn("truncated fixed64", self.failed(b"\x09\x00\x00"))
        self.assertIn("never closed", self.failed(key(2, 3) + pb_varint(1, 1)))
        self.assertIn("end-group", self.failed(key(2, 4) + pb_varint(1, 1)))
        self.assertIn("longer than 10", self.failed(b"\x08" + b"\xff" * 11 + b"\x01"))

    def test_trailing_garbage_fails(self):
        self.assertEqual(parse(S.PROTOBUF + b"\xff")[1], "failed")

    def test_short_or_weak_is_uncertain(self):
        self.assertIsNone(parse(b"\x08"))
        self.assertEqual(parse(pb_varint(1, 1))[1], "uncertain")
        self.assertEqual(summary(pb_varint(1, 1)), "protobuf? (1 field)")
        huge = pb_varint(500000, 1) + pb_varint(600000, 2) + pb_bytes(700000, b"text")
        self.assertEqual(parse(huge)[1], "uncertain")

    def test_text_wins_over_accidental_message(self):
        # b"Hi" is also the message {9: 105}; printable text starting >= 0x20 reads as text.
        data = pb_bytes(1, b"Hi") + pb_bytes(2, b"\x08\x01")
        node = parse(data)[2]
        self.assertEqual((node.children[0].kind, node.children[0].value), ("string", "Hi"))
        self.assertIn("also parses", node.children[0].note)
        self.assertEqual(node.children[1].kind, "protobuf")

    def test_random_bytes_are_not_nested_messages(self):
        # 16 bytes that happen to parse as one field numbered 96467: kept as bytes.
        uuidish = bytes.fromhex("9aad2e0cf4b6bef3a95e5f36d28b1e3c")
        rnd = key(96467, 2) + varint(12) + b"\x01" * 12
        data = pb_bytes(1, b"id") + pb_bytes(2, rnd) + pb_bytes(3, uuidish)
        node = parse(data)[2]
        self.assertEqual(node.children[1].kind, "bytes")

    def test_text_blob_is_not_offered_as_protobuf(self):
        root = decode_blob(b"hello there, this is plain text")
        self.assertEqual([c.kind for c in root.children], ["text"])


class LimitsTest(unittest.TestCase):
    def test_deep_nesting(self):
        data = pb_varint(1, 1)
        for _ in range(300):
            data = pb_bytes(1, data)
        t0 = time.time()
        root = decode_blob(data)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(root.children[0].kind, "protobuf")

    def test_many_fields_hit_the_node_budget(self):
        data = b"".join(pb_varint(1, i) for i in range(80000))
        t0 = time.time()
        attempt = parse(data)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(attempt[1], "uncertain")
        self.assertIn("decode limit", attempt[2].note)

    def test_interpretations_explain_failure(self):
        reasons = dict((a[0], a[3]) for a in interpretations(b"\x0e\x01\x02"))
        self.assertIn("wire type 6", reasons["protobuf"])


if __name__ == "__main__":
    unittest.main()
