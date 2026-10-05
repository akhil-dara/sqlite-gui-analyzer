"""Compression, text family, images, file signatures and typedstream."""
import base64
import gzip
import json
import time
import unittest
import zlib

try:
    import bz2
except ImportError:         # Python built without libbz2
    bz2 = None
try:
    import lzma
except ImportError:         # Python built without liblzma
    lzma = None

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests import decode_samples as S
from engine.decode import decode_blob, decoded_strings, interpretations, summary
from engine.decode import compress
from tests.helpers import within

MiB = 1 << 20


def kinds(node):
    return [c.kind for c in node.children]


def chain(data):
    """Kinds along the best interpretation, through transforms and their "bytes" output."""
    out, node = [], decode_blob(data)
    while node.children:
        best = node.children[0]
        out.append(best.kind)
        if best.children and best.children[0].kind == "bytes":
            node = best.children[0]
        else:
            break
    return out


class CompressionTest(unittest.TestCase):
    def test_every_codec_over_protobuf(self):
        cases = {"gzip": gzip.compress(S.PROTOBUF), "zlib": zlib.compress(S.PROTOBUF)}
        if bz2 is not None:
            cases["bz2"] = bz2.compress(S.PROTOBUF)
        if lzma is not None:
            cases["xz"] = lzma.compress(S.PROTOBUF)
            cases["lzma"] = lzma.compress(S.PROTOBUF, format=lzma.FORMAT_ALONE)
        for kind, data in cases.items():
            self.assertEqual(chain(data), [kind, "protobuf"], kind)
            node = decode_blob(data).children[0]
            self.assertEqual(node.confidence, "confident")
            self.assertEqual(node.children[0].value, S.PROTOBUF)
        self.assertEqual(summary(cases["gzip"]), "gzip → protobuf (7 fields)")

    def test_gzip_header_and_members(self):
        body = gzip.compress(b'{"a": 1}', mtime=1614834367)
        node = decode_blob(body + gzip.compress(b"")).children[0]
        self.assertIn("mtime 2021-03-04T05:06:07Z", node.note)
        self.assertIn("2 members", node.note)

    def test_raw_deflate_is_uncertain_and_needs_sensible_output(self):
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = co.compress(json.dumps({"k": ["v"] * 20}).encode()) + co.flush()
        root = decode_blob(data)
        self.assertEqual(kinds(root)[0], "deflate")
        self.assertEqual(root.children[0].confidence, "uncertain")
        self.assertEqual(root.children[0].children[0].children[0].kind, "json")
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        noise = co.compress(bytes(range(256)) * 3) + co.flush()
        self.assertNotIn("deflate", kinds(decode_blob(noise)))

    def test_truncated_streams(self):
        data = zlib.compress(" ".join(str(i * 7919) for i in range(3000)).encode())
        node = decode_blob(data[:len(data) // 2]).children[0]
        self.assertEqual((node.kind, node.confidence), ("zlib", "uncertain"))
        self.assertIn("ends early", node.note)
        for data in (gzip.compress(b"x")[:10], b"BZh9 not really", b"\x1f\x8b\x08junk"):
            self.assertNotIn(kinds(decode_blob(data))[:1], (["gzip"], ["bz2"]))

    def test_zstd_on_every_python(self):
        frame = b"\x28\xb5\x2f\xfd\x20\x05\x29\x00\x00hello"     # one raw block
        node = decode_blob(frame).children[0]
        self.assertEqual(node.kind, "zstd")
        self.assertEqual(node.children[0].value, b"hello")
        self.assertIn("zstd", summary(frame))
        try:
            from compression import zstd
        except ImportError:
            return
        data = zstd.compress(S.PROTOBUF)
        self.assertEqual(chain(data), ["zstd", "protobuf"])

    def test_decompression_bomb_is_capped(self):
        co = zlib.compressobj(9)
        chunk = b"\x00" * MiB
        bomb = b"".join(co.compress(chunk) for _ in range(200)) + co.flush()
        self.assertLess(len(bomb), 1 * MiB)
        t0 = time.time()
        root = decode_blob(bomb)
        elapsed = time.time() - t0
        node = root.children[0]
        self.assertEqual(node.kind, "zlib")
        self.assertIn("capped", node.note)
        self.assertEqual(len(node.children[0].value), 64 * MiB)
        self.assertIn("output", root.note)
        within(self, elapsed, 30)
        # summary() uses a much smaller cap.
        self.assertIn("zlib", summary(bomb))

    def test_nested_bombs_share_one_budget(self):
        co = zlib.compressobj(9)
        inner = b"".join(co.compress(b"\x00" * MiB) for _ in range(100)) + co.flush()
        data = gzip.compress(inner)
        root = decode_blob(data)
        total = sum(len(n.value) for n in root.walk() if n.kind == "bytes")
        self.assertLessEqual(total, len(data) + len(inner) + 128 * MiB)

    def test_inflate_stream_statuses(self):
        out, status, _members, _rest = compress.inflate_stream(
            "zlib", zlib.compress(b"a" * 100), 10)
        self.assertEqual((len(out), status), (10, "capped"))
        out, status, _members, rest = compress.inflate_stream(
            "zlib", zlib.compress(b"ab") + b"XY", 99)
        self.assertEqual((out, status, rest), (b"ab", "ok", b"XY"))


class TextTest(unittest.TestCase):
    def test_utf8_and_nul_terminator(self):
        node = decode_blob(b"caf\xc3\xa9 au lait\x00").children[0]
        self.assertEqual((node.kind, node.value), ("text", "café au lait"))
        self.assertIn("NUL-terminated", node.note)
        self.assertEqual(summary(b"hello"), "UTF-8 text: 'hello'")

    def test_utf16(self):
        for data, encoding in (("Hello, world".encode("utf-16-le"), "UTF-16LE"),
                               ("Hello, world".encode("utf-16-be"), "UTF-16BE"),
                               (b"\xff\xfe" + "Hi 世界".encode("utf-16-le"), "UTF-16LE")):
            node = decode_blob(data).children[0]
            self.assertEqual(node.kind, "text", data)
            self.assertTrue(node.note.startswith(encoding), node.note)
        self.assertEqual(summary("Hello, world".encode("utf-16-le")),
                         "UTF-16LE text: 'Hello, world'")

    def test_binary_is_not_text(self):
        root = decode_blob(bytes(range(256)))
        self.assertNotIn("text", kinds(root))
        self.assertEqual(decode_blob(b"\x00" * 64).children, [])

    def test_json(self):
        data = json.dumps({"a": [1, 2.5, True, None], "b": {"c": "d"}}).encode()
        root = decode_blob(data)
        self.assertEqual(kinds(root), ["json"])          # the text reading is superseded
        self.assertEqual(root.to_plain(), {"a": [1, 2.5, True, None], "b": {"c": "d"}})
        self.assertEqual(summary(data), "JSON object (2 keys)")
        attempts = interpretations(data)
        self.assertEqual([a[0] for a in attempts][:2], ["text", "json"])
        # Scalars only count as uncertain JSON; the text reading wins.
        self.assertEqual(kinds(decode_blob(b"12345")), ["text"])

    def test_json_keeps_duplicate_keys(self):
        node = decode_blob(b'{"k": 1, "k": 2}').children[0].children[0]
        self.assertEqual([(c.label, c.value) for c in node.children], [("k", 1), ("k", 2)])

    def test_base64_of_bplist(self):
        data = base64.b64encode(S.BPLIST)
        self.assertEqual(chain(data), ["base64", "bplist"])
        self.assertEqual(summary(data), "base64 → bplist: dict (4 keys)")
        wrapped = base64.encodebytes(S.BPLIST * 3)          # MIME line breaks
        self.assertEqual(chain(wrapped)[0], "base64")

    def test_base64_needs_meaningful_payload(self):
        for data in (b"deadbeefdeadbeefdeadbeef", b"abcdefghijklmnopqrstuvwx",
                     b"this is prose with spaces, not base64"):
            self.assertEqual(kinds(decode_blob(data)), ["text"], data)

    def test_strings_inside_structures_are_decoded(self):
        payload = {"blob": base64.b64encode(S.BPLIST).decode(),
                   "inner": json.dumps({"deep": "value"})}
        strings = decoded_strings(json.dumps(payload).encode())
        self.assertIn("Bob", strings)            # from the base64 bplist
        self.assertIn("deep", strings)           # from the JSON inside a string
        self.assertIn("value", strings)

    def test_str_input(self):
        root = decode_blob('{"x": "y"}')
        self.assertEqual(kinds(root), ["json"])
        self.assertIn("UTF-8", root.note)

    def test_uuid_and_int64_time(self):
        u = bytes.fromhex("17a02160bcea428cb20d2b031053b7f6")
        node = decode_blob(u).children[0]
        self.assertEqual((node.kind, node.value, node.confidence),
                         ("uuid", "17a02160-bcea-428c-b20d-2b031053b7f6", "uncertain"))
        filetime = ((1614834367 + 11644473600) * 10 ** 7).to_bytes(8, "little")
        node = decode_blob(filetime).children[0]
        self.assertEqual((node.kind, node.value), ("date", "2021-03-04T05:06:07Z"))
        self.assertIn("FILETIME", node.note)


class ImageAndFileTest(unittest.TestCase):
    def test_dimensions(self):
        cases = {"PNG": S.png(3, 2), "GIF": S.gif(640, 480), "JPEG": S.jpeg(800, 600),
                 "BMP": S.bmp(10, 20), "WEBP": S.webp_lossless(123, 45),
                 "TIFF": S.tiff(7, 9)}
        expected = {"PNG": (3, 2), "GIF": (640, 480), "JPEG": (800, 600), "BMP": (10, 20),
                    "WEBP": (123, 45), "TIFF": (7, 9)}
        for name, data in cases.items():
            node = decode_blob(data).children[0]
            self.assertEqual((node.kind, node.value, node.confidence),
                             ("image", name, "confident"), name)
            self.assertEqual(dict((c.label, c.value) for c in node.children),
                             {"width": expected[name][0], "height": expected[name][1]})
        self.assertEqual(summary(S.png(640, 480)), "PNG 640×480")

    def test_truncated_image_is_uncertain(self):
        node = decode_blob(b"\xff\xd8\xff\xe0\x00").children[0]
        self.assertEqual((node.kind, node.confidence), ("image", "uncertain"))

    def test_file_signatures(self):
        self.assertEqual(summary(b"%PDF-1.7\n..."), "PDF document")
        self.assertEqual(summary(b"SQLite format 3\x00" + b"\x00" * 100), "SQLite database")
        self.assertEqual(decode_blob(b"PK\x03\x04" + b"\x00" * 30).children[0].kind, "file")
        # Short ASCII signatures must not swallow ordinary text.
        self.assertEqual(kinds(decode_blob(b"caffeine is great")), ["text"])


class TypedstreamTest(unittest.TestCase):
    def test_attributed_body(self):
        root = decode_blob(S.TYPEDSTREAM)
        ts = root.children[0]
        self.assertEqual((ts.kind, ts.confidence, ts.value),
                         ("typedstream", "confident", S.TS_TEXT))
        top = ts.children[0]
        self.assertEqual((top.kind, top.value), ("object", "NSMutableAttributedString"))
        self.assertIn("NSAttributedString", top.note)
        text = top.children[0]
        self.assertEqual((text.kind, text.label, text.value), ("string", "string", S.TS_TEXT))
        self.assertEqual(S.TYPEDSTREAM[text.offset:text.offset + text.length],
                         S.TS_TEXT.encode())
        attrs = [c for c in top.children if c.kind == "dict"][0]
        entry = attrs.children[0]
        self.assertEqual((entry.label, entry.kind, entry.value),
                         ("__kIMMessagePartAttributeName", "int", 0))
        self.assertEqual(summary(S.TYPEDSTREAM),
                         "typedstream NSMutableAttributedString: 'Hi there :)'")
        self.assertEqual(decoded_strings(S.TYPEDSTREAM),
                         [S.TS_TEXT, "__kIMMessagePartAttributeName"])

    def test_object_references(self):
        ts = decode_blob(S.TYPEDSTREAM_REFS).children[0]
        array = ts.children[0]
        self.assertEqual((array.kind, array.value), ("array", "NSArray"))
        self.assertEqual([c.value for c in array.children], ["hello", "hello"])
        self.assertIn("repeat of object #3", array.children[1].note)

    def test_truncated_keeps_text(self):
        cut = S.TYPEDSTREAM.index(b"iI") + 2
        ts = decode_blob(S.TYPEDSTREAM[:cut]).children[0]
        self.assertEqual((ts.kind, ts.value), ("typedstream", S.TS_TEXT))
        self.assertIn("stopped", ts.note)
        for n in range(len(S.TYPEDSTREAM)):
            decode_blob(S.TYPEDSTREAM[:n])
            summary(S.TYPEDSTREAM[:n])


if __name__ == "__main__":
    unittest.main()
