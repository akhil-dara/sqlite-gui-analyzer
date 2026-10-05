"""Public decode API: shapes, limits, determinism, and never raising on hostile input."""
import base64
import gzip
import json
import random
import time
import unittest
import zlib
from unittest import mock

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests import decode_samples as S
from engine.decode import (Node, decode_blob, decoded_strings, interpretations, summary)
from engine.fileformat.record import InvalidText


def samples():
    base = {
        "bplist": S.BPLIST, "xml": S.XML_PLIST, "archive": S.sample_archive(),
        "protobuf": S.PROTOBUF, "gzip": gzip.compress(S.PROTOBUF),
        "zlib": zlib.compress(S.sample_archive()), "typedstream": S.TYPEDSTREAM,
        "typedstream_refs": S.TYPEDSTREAM_REFS,
        "json": json.dumps({"a": [1, {"b": base64.b64encode(S.BPLIST).decode()}]}).encode(),
        "base64": base64.b64encode(gzip.compress(S.BPLIST)),
        "utf16": "Hello UTF-16 text".encode("utf-16-le"), "png": S.png(4, 4),
        "jpeg": S.jpeg(10, 10), "cyclic": S.cyclic_bplist(),
    }
    try:
        import bz2
        import lzma
        base["bz2"] = bz2.compress(S.PROTOBUF)
        base["xz"] = lzma.compress(S.BPLIST)
    except ImportError:
        pass
    return base


def check_all_apis(test, data):
    root = decode_blob(data)
    test.assertIsInstance(root, Node)
    json.dumps(root.to_plain())
    line = summary(data)
    test.assertIsInstance(line, str)
    test.assertNotIn("undecodable", line)
    strings = decoded_strings(data)
    test.assertTrue(all(isinstance(s, str) for s in strings))
    for attempt in interpretations(data):
        test.assertEqual(len(attempt), 4)
        test.assertIn(attempt[1], ("confident", "uncertain", "failed"))
    test.assertNotIn("decode error", root.note)
    test.assertNotIn("decoder error", root.note)
    return root


class ShapeTest(unittest.TestCase):
    def test_root_is_bytes_node(self):
        root = decode_blob(S.BPLIST)
        self.assertEqual((root.kind, root.value, root.offset, root.length),
                         ("bytes", S.BPLIST, 0, len(S.BPLIST)))
        self.assertEqual(root.confidence, "confident")

    def test_interpretations_ranked(self):
        attempts = interpretations(S.PROTOBUF)
        ranks = [{"confident": 0, "uncertain": 1, "failed": 2}[a[1]] for a in attempts]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(attempts[0][0], "protobuf")
        for kind, confidence, node, reason in attempts:
            self.assertIsInstance(reason, str)
            self.assertEqual(node is None, confidence == "failed")

    def test_max_depth(self):
        data = S.sample_archive()
        self.assertEqual(decode_blob(data, max_depth=0).children, [])
        shallow = decode_blob(data, max_depth=1)
        nsdata = [n for n in shallow.walk() if n.kind == "bytes" and n is not shallow]
        self.assertTrue(nsdata)
        self.assertTrue(all(not n.children for n in nsdata))
        self.assertIn("depth limit", nsdata[0].note)
        self.assertEqual(decode_blob(data, max_depth="junk").children[0].kind, "bplist")

    def test_node_helpers(self):
        root = decode_blob(S.TYPEDSTREAM)
        self.assertEqual(root.find("typedstream").value, S.TS_TEXT)
        self.assertIsNone(root.find("no-such-kind"))
        depths = [d for d, _ in root.walk(with_depth=True)]
        self.assertEqual(depths[0], 0)
        self.assertEqual(list(root.text_leaves())[0], S.TS_TEXT)

    def test_summary_edge_cases(self):
        self.assertEqual(summary(b""), "empty")
        self.assertEqual(summary(b"\x00" * 10), "binary data (10 bytes)")
        self.assertEqual(summary(None), "null")
        self.assertEqual(decode_blob(b"").children, [])

    def test_to_plain(self):
        inner = json.dumps({"deep": 1})
        plain = decode_blob(json.dumps({"s": inner, "k": 2}).encode()).to_plain()
        self.assertEqual(plain, {"s": inner, "k": 2})         # strings keep their text
        self.assertEqual(decode_blob(S.png(3, 2)).to_plain(),
                         {"$image": "PNG", "width": 3, "height": 2})
        archive = decode_blob(S.sample_archive()).to_plain()
        self.assertEqual(list(archive), ["root"])
        self.assertEqual(archive["root"]["thing"], {"$class": "MyThing", "title": "Alice",
                                                    "count": 5})
        self.assertEqual(decode_blob(b'{"k": 1, "k": 2}').to_plain(), {"k": 1, "k#1": 2})
        self.assertEqual(decode_blob(b"\x00\x01").to_plain(), {"$bytes": "0001", "$length": 2})

    def test_one_failing_decoder_does_not_lose_the_rest(self):
        with mock.patch("engine.decode.protobuf.decode", side_effect=ValueError("boom")):
            attempts = interpretations(S.BPLIST)
            self.assertEqual(attempts[0][0], "bplist")
            self.assertIn("decoder error: ValueError: boom", [a[3] for a in attempts])
            data = bytes(range(1, 40))
            self.assertIsInstance(summary(data), str)
            self.assertIn("decoder error", decode_blob(data).note)

    def test_decoded_strings_limit_and_dedupe(self):
        data = json.dumps(["same"] * 50 + ["s%d" % i for i in range(100)]).encode()
        strings = decoded_strings(data)
        self.assertEqual(strings.count("same"), 1)
        self.assertEqual(len(decoded_strings(data, limit=5)), 5)


class RobustnessTest(unittest.TestCase):
    def test_samples_decode_cleanly(self):
        for name, data in samples().items():
            with self.subTest(name):
                root = check_all_apis(self, data)
                self.assertTrue(root.children, name)
                self.assertTrue(all(c.confidence == "confident" for c in root.children), name)

    def test_truncated_and_corrupted_samples_never_raise(self):
        rng = random.Random(7)
        for name, data in samples().items():
            cuts = sorted(set([1, 2, 3, 8, 9, 16, len(data) // 3, len(data) // 2,
                               len(data) - 33, len(data) - 1]))
            for cut in cuts:
                if 0 < cut < len(data):
                    check_all_apis(self, data[:cut])
            for _ in range(12):
                mutated = bytearray(data)
                for _ in range(rng.randint(1, 4)):
                    mutated[rng.randrange(len(mutated))] = rng.randrange(256)
                check_all_apis(self, bytes(mutated))

    def test_random_bytes_never_raise(self):
        rng = random.Random(11)
        for size in list(range(0, 40)) + [100, 1000, 5000]:
            for _ in range(5):
                check_all_apis(self, bytes(rng.randrange(256) for _ in range(size)))

    def test_any_input_type(self):
        for value in (None, 0, -5, 3.25, float("nan"), True, "text", "\ud800", bytearray(b"ab"),
                      memoryview(b"{}"), InvalidText(b"\xff\xfe"), object(), [1], {"a": 1}):
            root = decode_blob(value)
            self.assertIsInstance(root, Node)
            self.assertIsInstance(summary(value), str)
            self.assertIsInstance(decoded_strings(value), list)
            self.assertIsInstance(interpretations(value), list)

    def test_deterministic(self):
        for name, data in samples().items():
            first = json.dumps(decode_blob(data).to_plain(), sort_keys=True)
            again = json.dumps(decode_blob(data).to_plain(), sort_keys=True)
            self.assertEqual(first, again, name)
            self.assertEqual(summary(data), summary(data))
            self.assertEqual([a[:2] + (a[3],) for a in interpretations(data)],
                             [a[:2] + (a[3],) for a in interpretations(data)])


class LimitsTest(unittest.TestCase):
    def test_deep_json(self):
        for data in (b"[" * 100000 + b"]" * 100000, b'{"a":' * 5000 + b"1" + b"}" * 5000):
            t0 = time.time()
            check_all_apis(self, data)
            self.assertLess(time.time() - t0, 10)

    def test_node_budget(self):
        data = json.dumps(list(range(200000))).encode()
        t0 = time.time()
        root = decode_blob(data)
        self.assertLess(time.time() - t0, 10)
        self.assertLessEqual(sum(1 for _ in root.walk()), 50010)
        self.assertIn("nodes", root.note)

    def test_nested_blob_chain(self):
        data = S.BPLIST
        for _ in range(10):
            data = base64.b64encode(zlib.compress(data))
        root = decode_blob(data)
        kinds = [n.kind for n in root.walk()]
        self.assertIn("base64", kinds)
        self.assertNotIn("bplist", kinds)        # deeper than max_depth=6
        self.assertIn("bplist", [n.kind for n in decode_blob(
            base64.b64encode(zlib.compress(S.BPLIST))).walk()])


if __name__ == "__main__":
    unittest.main()
