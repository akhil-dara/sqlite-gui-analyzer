"""BLOBs saved as files: as stored, decoded as JSON, or plists as XML property lists; and
the view each format gets (a protobuf is never dressed up as a plist)."""

import gzip
import json
import os
import plistlib
import unittest

from tests.helpers import TempDirTest
from tests.decode_samples import pb_bytes, pb_varint
from engine.decode import decode_blob
from engine.decode.render import decoded_file, first_format, format_of, protobuf_text
from utils import blob_type, export_row_blobs

PLIST = plistlib.dumps({"name": "Ann", "count": 3}, fmt=plistlib.FMT_BINARY)
PROTO = pb_varint(1, 150) + pb_bytes(2, b"hello") + pb_bytes(3, pb_varint(1, 7))
JSON = b'{"a": [1, 2]}'
RAW = bytes(range(7, 40))


class RenderTest(unittest.TestCase):
    def test_protobuf_reads_like_decode_raw(self):
        root = decode_blob(PROTO)
        pb = root.children[0]
        self.assertEqual(pb.kind, "protobuf")
        text = protobuf_text(pb)
        self.assertIn("1: 150", text)
        self.assertIn('2: "hello"', text)
        self.assertIn("3 {\n  1: 7\n}", text)
        self.assertEqual(format_of([root, pb]), "protobuf")

    def test_plists_are_found_through_compression(self):
        for blob in (PLIST, gzip.compress(PLIST, mtime=0)):
            root = decode_blob(blob)
            self.assertEqual(first_format(root).kind, "bplist")
            self.assertEqual(format_of([root, root.children[0]]), "plist")
        self.assertIsNone(first_format(decode_blob(PROTO)) and None)
        self.assertNotEqual(first_format(decode_blob(PROTO)).kind, "bplist")

    def test_decoded_file_per_mode(self):
        content, ext = decoded_file(PLIST, "xml")
        self.assertEqual(ext, ".xml.plist")
        self.assertEqual(plistlib.loads(content), {"name": "Ann", "count": 3})
        self.assertEqual(decoded_file(PROTO, "xml"), (None, "not a plist"))
        self.assertEqual(decoded_file(JSON, "xml"), (None, "not a plist"))
        content, ext = decoded_file(PROTO, "json")
        self.assertEqual(ext, ".json")
        self.assertEqual(json.loads(content)["2"], "hello")
        self.assertEqual(decoded_file(RAW, "json"), (None, "not decoded"))

    def test_quick_label_is_not_fooled_by_hashes(self):
        import hashlib
        import uuid
        self.assertEqual(blob_type(hashlib.sha1(b"x").digest()), "BLOB")
        self.assertEqual(blob_type(uuid.UUID(int=0x6abcf43d5dc644e588d0fb1487b17df7).bytes),
                         "BLOB")
        self.assertEqual(blob_type(b"plain text in a blob"), "BLOB")
        self.assertEqual(blob_type(PROTO), "Protobuf?")
        self.assertEqual(blob_type(bytes.fromhex("0a0418012001")), "Protobuf?")

    def test_json_kept_as_a_json_string_reads_as_json(self):
        from engine.decode import summary
        blob = b'"{\\"carousel\\":{\\"on\\":true}}"'
        self.assertEqual(decode_blob(blob).children[0].kind, "json")
        self.assertEqual(decode_blob(blob).to_plain(), {"carousel": {"on": True}})
        self.assertIn("JSON", summary(blob))


class ExportTest(TempDirTest):
    rows = [[1, PLIST, PROTO], [2, JSON, RAW], [3, gzip.compress(PLIST, mtime=0), None]]
    cols = ["_rid", "a", "b"]

    def export(self, **kw):
        out = os.path.join(self.tmp, "out_%d" % len(os.listdir(self.tmp)))
        os.makedirs(out)
        files, skipped = [], {}
        res = export_row_blobs(out, "t", self.cols, iter(self.rows), files=files,
                               skipped=skipped, **kw)
        return res, sorted(os.listdir(out)), files, skipped

    def test_raw(self):
        (n, errors, _), names, files, _ = self.export()
        self.assertEqual((n, errors), (5, 0))
        self.assertEqual(len(names), 5)
        self.assertEqual(len(files), 5)

    def test_json(self):
        (n, errors, _), names, _, _ = self.export(mode="json")
        self.assertEqual((n, errors), (5, 0))
        self.assertEqual(sum(x.endswith(".json") for x in names), 4)    # RAW stays bytes
        self.assertEqual(sum(x.endswith(".bin") for x in names), 1)

    def test_xml_plists_only_with_the_bytes_beside(self):
        (n, errors, _), names, files, skipped = self.export(mode="xml", keep_raw=True)
        self.assertEqual((n, errors), (2, 0))                   # two plists (one gzipped)
        self.assertEqual(sum(x.endswith(".xml.plist") for x in names), 2)
        self.assertEqual(len(names), 4)                         # + the original bytes
        self.assertEqual(sum(skipped.values()), 3)              # protobuf, JSON, bytes
        self.assertEqual(len(files), 4)                         # all in the manifest

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            export_row_blobs(self.tmp, "t", self.cols, iter(self.rows), mode="yaml")


if __name__ == "__main__":
    unittest.main()
