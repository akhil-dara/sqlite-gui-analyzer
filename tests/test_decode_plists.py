"""Property lists and NSKeyedArchiver archives."""
import datetime
import json
import plistlib
import time
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests import decode_samples as S
from engine.decode import decode_blob, decoded_strings, interpretations, summary

UID = plistlib.UID


def only_child(node):
    assert len(node.children) == 1, node.children
    return node.children[0]


class PlainPlistTest(unittest.TestCase):
    def test_binary(self):
        root = decode_blob(S.BPLIST)
        plist = only_child(root)
        self.assertEqual((plist.kind, plist.confidence, plist.offset, plist.length),
                         ("bplist", "confident", 0, len(S.BPLIST)))
        top = only_child(plist)
        self.assertEqual(top.kind, "dict")
        self.assertEqual(plist.to_plain(), {"name": "Bob", "age": 42, "tags": ["a", "b"],
                                            "blob": {"$bytes": "0001", "$length": 2}})
        self.assertEqual(summary(S.BPLIST), "bplist: dict (4 keys)")

    def test_xml(self):
        root = decode_blob(S.XML_PLIST)
        plist = only_child(root)
        self.assertEqual(plist.kind, "xml_plist")
        self.assertEqual(plist.to_plain()["tags"], ["a", "b"])
        self.assertEqual(summary(S.XML_PLIST), "XML plist: dict (4 keys)")
        # The XML is text too, but the plist reading wins in the tree...
        self.assertEqual([c.kind for c in root.children], ["xml_plist"])
        # ...while the inspector still lists the text reading.
        kinds = [a[0] for a in interpretations(S.XML_PLIST)]
        self.assertEqual(kinds[0], "xml_plist")
        self.assertIn("text", kinds)

    def test_dates_and_nested_plist_data(self):
        inner = plistlib.dumps(["x", 1], fmt=plistlib.FMT_BINARY)
        data = plistlib.dumps({"d": datetime.datetime(2020, 1, 2, 3, 4, 5), "inner": inner},
                              fmt=plistlib.FMT_BINARY)
        plist = only_child(decode_blob(data))
        top = only_child(plist)
        by_key = dict((c.label, c) for c in top.children)
        self.assertEqual((by_key["d"].kind, by_key["d"].value), ("date", "2020-01-02T03:04:05Z"))
        nested = by_key["inner"]
        self.assertEqual(nested.kind, "bytes")
        self.assertEqual(only_child(nested).kind, "bplist")
        self.assertEqual(nested.to_plain(), ["x", 1])

    def test_unsupported_version_and_bad_trailer(self):
        for data in (b"bplist15" + b"\x00" * 60, S.BPLIST[:-10], S.BPLIST[:20],
                     S.BPLIST[:-32] + b"\xff" * 32):
            attempts = interpretations(data)
            self.assertTrue(any(a[0] == "bplist" and a[1] == "failed" for a in attempts), data)
            root = decode_blob(data)
            self.assertFalse(any(c.kind == "bplist" for c in root.children))

    def test_reference_cycle_in_binary_plist(self):
        data = S.cyclic_bplist()
        plist = only_child(decode_blob(data))
        top = only_child(plist)
        self.assertEqual(top.kind, "array")
        self.assertIn("cycle", only_child(top).note)


class KeyedArchiveTest(unittest.TestCase):
    def setUp(self):
        self.data = S.sample_archive()
        self.root = decode_blob(self.data)
        self.plist = only_child(self.root)
        self.archive = only_child(self.plist)
        self.top = only_child(self.archive)
        self.fields = dict((c.label, c) for c in self.top.children)

    def test_shape(self):
        self.assertEqual((self.plist.kind, self.archive.kind), ("bplist", "nskeyedarchive"))
        self.assertEqual(self.archive.value, "NSKeyedArchiver")
        self.assertEqual((self.top.label, self.top.kind, self.top.value),
                         ("root", "dict", "NSMutableDictionary"))
        self.assertEqual(summary(self.data),
                         "bplist: NSKeyedArchiver NSMutableDictionary (8 keys)")

    def test_values(self):
        f = self.fields
        self.assertEqual((f["name"].kind, f["name"].value), ("string", "Alice"))
        self.assertEqual((f["when"].kind, f["when"].value), ("date", "2020-01-06T10:40:00Z"))
        self.assertEqual(f["link"].value, "https://example.org/a?b=c")
        self.assertEqual(f["id"].value, "00010203-0405-0607-0809-0a0b0c0d0e0f")
        self.assertEqual(f["nothing"].kind, "null")
        thing = f["thing"]
        self.assertEqual((thing.kind, thing.value), ("object", "MyThing"))
        self.assertEqual(dict((c.label, c.value) for c in thing.children),
                         {"title": "Alice", "count": 5})

    def test_nsdata_is_decoded_again(self):
        blob = self.fields["blob"]
        self.assertEqual(blob.kind, "bytes")
        self.assertEqual(blob.value, S.BPLIST)
        self.assertEqual(only_child(blob).kind, "bplist")
        self.assertIn("Bob", decoded_strings(self.data))

    def test_array_of_dict_and_cycle(self):
        items = self.fields["items"]
        self.assertEqual((items.kind, items.value), ("array", "NSArray"))
        first, second = items.children
        self.assertEqual((first.kind, first.children[0].label, first.children[0].value),
                         ("dict", "k", 42))
        self.assertEqual(second.kind, "uid")
        self.assertIn("cycle", second.note)

    def test_decoded_strings_and_plain(self):
        strings = decoded_strings(self.data)
        for s in ("name", "Alice", "k", "https://example.org/a?b=c", "Bob", "tags"):
            self.assertIn(s, strings)
        self.assertNotIn("$null", strings)
        self.assertNotIn("NSMutableDictionary", strings)      # class names are structure
        json.dumps(self.root.to_plain())

    def test_raw_view_only_in_interpretations(self):
        attempts = [a for a in interpretations(self.data) if a[0] == "bplist"]
        self.assertEqual(len(attempts), 2)
        resolved, raw = attempts
        self.assertTrue(resolved.primary)
        self.assertFalse(raw.primary)
        self.assertEqual(raw[2].children[0].kind, "dict")      # $archiver/$top/$objects

    def test_xml_keyed_archive_with_cf_uid(self):
        xml = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000,
                              "$top": {"root": {"CF$UID": 1}},
                              "$objects": ["$null", {"$class": {"CF$UID": 2},
                                                     "NS.string": "hello"},
                                           {"$classname": "NSString",
                                            "$classes": ["NSString", "NSObject"]}]},
                             fmt=plistlib.FMT_XML)
        archive = only_child(only_child(decode_blob(xml)))
        self.assertEqual(archive.kind, "nskeyedarchive")
        self.assertEqual(only_child(archive).value, "hello")

    def test_hostile_archives_terminate(self):
        # UID pointing at itself, out-of-range UIDs, a missing class.
        objects = ["$null", {"$class": UID(9), "self": UID(1), "bad": UID(99)}, UID(2)]
        data = S.keyed_archive(objects, top={"root": UID(1), "loop": UID(2)})
        root = decode_blob(data)
        notes = " ".join(n.note for n in root.walk())
        self.assertIn("cycle", notes)
        self.assertIn("outside $objects", notes)
        # A DAG that doubles at every level would be 2^40 nodes if expanded naively.
        objects = ["$null"]
        for i in range(1, 41):
            objects.append({"$class": UID(42), "NS.objects": [UID(i + 1), UID(i + 1)]})
        objects.append("leaf")
        objects.append({"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]})
        data = S.keyed_archive(objects)
        t0 = time.time()
        root = decode_blob(data)
        self.assertLess(time.time() - t0, 5)
        self.assertLess(sum(1 for _ in root.walk()), 60000)


if __name__ == "__main__":
    unittest.main()
