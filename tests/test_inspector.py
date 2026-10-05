import gzip
import plistlib
import time
import unittest

from tests import helpers  # noqa: F401  (sets sys.path)
from tests.decode_samples import pb_bytes, pb_varint, png
from inspector import BlobInspector, DecodedTree, node_value_text, number_of
from engine.decode import decode_blob
from engine.decode.nodes import Node


def nested_blob():
    """gzip -> protobuf{1: 1600000000, 2: 'hello inspector', 3: bplist{'key': 'deep value'}}"""
    inner = plistlib.dumps({"key": "deep value"}, fmt=plistlib.FMT_BINARY)
    message = pb_varint(1, 1600000000) + pb_bytes(2, b"hello inspector") + pb_bytes(3, inner)
    return gzip.compress(message, mtime=0), message


class ModelTest(unittest.TestCase):
    def test_buffers_paths_and_byte_lookup(self):
        blob, message = nested_blob()
        model = DecodedTree(decode_blob(blob))
        text = next(n for n in model.root.walk() if n.value == "hello inspector")
        buf = model.buffer_of(text)
        self.assertEqual(buf.kind, "bytes")
        self.assertEqual(bytes(buf.value), message)            # the gzip output, not the BLOB
        field = buf.value[text.offset:text.offset + text.length]         # key, length, payload
        self.assertEqual(field[-15:], b"hello inspector")
        self.assertIs(model.node_at(buf, text.offset + 3), text)
        kinds = [n.kind for n in model.path(text)]
        self.assertEqual(kinds[:4], ["bytes", "gzip", "bytes", "protobuf"])
        deep = next(n for n in model.root.walk() if n.value == "deep value")
        self.assertIn("bplist", [n.kind for n in model.path(deep)])

    def test_value_text_and_numbers(self):
        self.assertEqual(node_value_text(Node("bytes", value=b"x" * 2048)), "2.0KB")
        self.assertEqual(node_value_text(Node("dict", value="NSDictionary", children=[Node("int")] * 2)),
                         "NSDictionary (2 keys)")
        self.assertEqual(node_value_text(Node("string", value="a\nb")), "a\\nb")
        self.assertEqual(number_of(Node("int", value=5)), 5)
        self.assertEqual(number_of(Node("string", value="1.5e9")), 1.5e9)
        self.assertIsNone(number_of(Node("bool", value=True)))
        self.assertEqual(number_of(Node("bytes", value=b"\x01\x00\x00\x00")), 1)


class InspectorWindowTest(unittest.TestCase):
    def setUp(self):
        self.root = helpers.tk_root(self)

    def open(self, data):
        win = BlobInspector(self.root, data, "payload", "t.payload row 1")
        win.geometry("1000x600+-4000+0")
        deadline = time.time() + 20
        while win._tree_model is None and time.time() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertIsNotNone(win._tree_model, "decode did not finish")
        self.root.update()
        return win

    def test_nested_blob_selection_follows_bytes_both_ways(self):
        blob, message = nested_blob()
        win = self.open(blob)
        self.assertIn("gzip", win._summary_lbl.cget("text"))
        text = next(n for n in win._tree_model.root.walk() if n.value == "hello inspector")
        win.reveal(text)
        self.root.update()
        self.assertIs(win.selected_node(), text)
        self.assertEqual(win.hex.data, message)                # hex shows the gzip output
        hl = win.hex.text.tag_ranges("hl")
        self.assertTrue(hl)
        self.assertIn("hello inspector", win.text.get("1.0", "end"))
        self.assertIn("›", win._path_lbl.cget("text"))
        # clicking another byte of the buffer selects the value stored there
        number = next(n for n in win._tree_model.root.walk() if n.value == 1600000000)
        win._on_byte(number.offset)
        self.root.update()
        self.assertIs(win.selected_node(), number)
        rows = [win.times.item(i, "values") for i in win.times.get_children()]
        self.assertIn(("Unix seconds", "2020-09-13 12:26:40 UTC"), [tuple(r) for r in rows])
        likely = [win.times.item(i, "values")[0] for i in win.times.get_children()
                  if "likely" in win.times.item(i, "tags")]
        self.assertIn("Unix seconds", likely)
        win.destroy()

    def test_find_and_interpretations(self):
        blob, _message = nested_blob()
        win = self.open(blob)
        win._find_var.set("deep val")
        win._find_next()
        self.root.update()
        self.assertEqual(win.selected_node().value, "deep value")
        self.assertGreaterEqual(len(win._interp.cget("values")), 2)
        win._interp.set(win._interp.cget("values")[1])
        win._on_interpretation()
        self.root.update()
        self.assertEqual(win._tree_model.root.kind, "bytes")
        win.destroy()

    def test_plain_bytes_and_image(self):
        win = self.open(b"\x00\x01\x02 no structure here \xff")
        self.assertTrue(win.tree.get_children())
        win.destroy()
        win = self.open(png(3, 2))
        tabs = [win.tabs.tab(t, "text") for t in win.tabs.tabs()]
        self.assertIn("Image", tabs)
        win.destroy()

    def decoded_view(self, data, saved):
        """(view used, text shown, note) of a BLOB opened with `saved` as the kept view."""
        from types import SimpleNamespace
        self.root.tags = SimpleNamespace(settings={"blob_view": saved},
                                         save_settings=lambda: None)
        win = self.open(data)
        node = win.selected_node()
        view, _note = win.view_for(node)
        out = (view, win.text.get("1.0", "end-1c"), win._view_note.cget("text"))
        win.destroy()
        return out

    def test_each_format_opens_in_its_own_view(self):
        message = pb_varint(1, 150) + pb_bytes(2, b"hello") + \
            pb_bytes(3, pb_varint(1, 7) + pb_bytes(2, b"inner"))
        plist = plistlib.dumps({"name": "Ann", "n": 3}, fmt=plistlib.FMT_BINARY)
        for saved in ("auto", "xml", "json", "text", "protobuf"):
            view, text, note = self.decoded_view(message, saved)
            # a protobuf is never shown as a plist, whatever view was kept
            self.assertNotIn("<plist", text, saved)
            if saved in ("auto", "xml", "protobuf"):
                self.assertEqual(view, "protobuf", saved)
                self.assertIn('2: "hello"', text)
                self.assertIn("3 {", text)
            if saved == "xml":
                self.assertIn("XML is for plists", note)
            view, text, note = self.decoded_view(plist, saved)
            if saved in ("auto", "xml"):
                self.assertEqual(view, "xml", saved)
                self.assertIn("<plist", text)
            if saved == "protobuf":
                self.assertIn("for protobuf", note)
        view, text, _ = self.decoded_view(gzip.compress(plist, mtime=0), "auto")
        self.assertEqual(view, "xml")                   # a plist inside gzip is still a plist
        view, text, _ = self.decoded_view(b'{"a": [1, 2]}', "xml")
        self.assertEqual(view, "json")
        self.assertIn('"a"', text)
        view, text, _ = self.decoded_view(b'"{\\"carousel\\":{\\"on\\":true}}"', "auto")
        self.assertEqual(view, "json")
        self.assertIn('"carousel"', text)               # JSON kept as a JSON string, opened
        view, text, _ = self.decoded_view("plain words, nothing else".encode(), "auto")
        self.assertEqual(view, "text")
        self.assertEqual(text, "plain words, nothing else")


if __name__ == "__main__":
    unittest.main()
