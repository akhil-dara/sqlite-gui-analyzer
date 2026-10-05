"""The decoded-tree data model shared by every decoder.

A decode produces a tree of Node objects. The tree always starts at a "bytes" node that
holds a byte buffer; its children are the interpretations chosen for that buffer (e.g. a
"bplist" node, a "gzip" node). Inside an interpretation, any byte string that is itself worth
decoding (NSData, protobuf bytes fields, decompressed output, base64 payloads) becomes another
"bytes" node with its own interpretations.

Coordinates: `offset`/`length` of a node are positions in the buffer of the nearest ancestor
"bytes" node (a "bytes" node's own offset/length are in *its* parent buffer, and when known
they locate exactly its `value` there). They are None when the decoder cannot know them
(values inside property lists and JSON, output of decompression or base64).

Kinds, and what `value` and `children` hold:
  bytes          value: the buffer. children: its chosen interpretations (see __init__).
  bplist, xml_plist
                 children: [top value] or, for an archive, [nskeyedarchive].
  nskeyedarchive value: the $archiver name. children: one per $top key (label = key).
  json           children: [top value]. note: shape.
  protobuf       children: fields (label = field number); nested messages are "protobuf".
  typedstream    value: the first string (message text). children: top-level values.
  gzip, zlib, deflate, bz2, xz, lzma, zstd, lz4, lz4_apple, lzfse, lzvn, base64
                 children: [bytes] with the output; note: sizes, members, limits hit.
                 (lzma / xz have no children when the stream needs more memory than the
                 limit decode_lzma_memory; the note says so.)
  image          value: "PNG", "JPEG", ... children: width, height ints. note: "W×H".
  file           value: file type recognised by signature (PDF, SQLite, ZIP, ...).
  text           value: str. note: encoding (UTF-8 / UTF-16LE / UTF-16BE) and details.
  uuid           value: canonical UUID string (always uncertain).
  dict           value: class name or None. children labelled by key.
  array          value: class name or None. children labelled by index.
  object         value: class name. children: archived fields. note: class chain.
  string, int, float, bool
                 value: the scalar (strings may have a json/base64 child when they hold one).
  date           value: ISO 8601 UTC string. note: source value and epoch.
  uid            value: object number (an unresolved, repeated or cyclic reference).
  null           no value.
"""

import binascii

CONFIDENT, UNCERTAIN, FAILED = "confident", "uncertain", "failed"

# Kinds whose `value` is text that search and text_leaves() should see.
TEXT_KINDS = frozenset(("string", "text"))

HEX_PREVIEW = 4096          # bytes shown as hex by to_plain()


class Node(object):
    """One element of a decoded tree.

    kind        what this is: a format ("bplist", "gzip", "protobuf", ...), a container
                ("dict", "array", "object"), a scalar ("string", "int", ...) or "bytes".
    label       dictionary key, protobuf field number, list index or role name (or None).
    value       scalar payload; the raw buffer for "bytes"; a class name for containers.
    children    list of child nodes (possibly empty).
    offset      start in the nearest ancestor "bytes" buffer, or None when unknown.
    length      byte length there, or None.
    confidence  "confident" or "uncertain".
    note        short free text: alternatives, limits hit, class chain, encoding...
    """

    __slots__ = ("kind", "label", "value", "children", "offset", "length", "confidence", "note")

    def __init__(self, kind, label=None, value=None, children=None, offset=None, length=None,
                 confidence=CONFIDENT, note=""):
        self.kind = kind
        self.label = label
        self.value = value
        self.children = children if children is not None else []
        self.offset = offset
        self.length = length
        self.confidence = confidence
        self.note = note

    def __repr__(self):
        v = self.value
        if isinstance(v, (bytes, bytearray)):
            v = "<%d bytes>" % len(v)
        elif isinstance(v, str) and len(v) > 40:
            v = v[:40] + "..."
        return "Node(%s, label=%r, value=%r, children=%d)" % (self.kind, self.label, v,
                                                              len(self.children))

    def walk(self, with_depth=False):
        """Pre-order traversal without recursion (trees can be deep).

        Yields nodes, or (depth, node) pairs when with_depth is true.
        """
        stack = [(0, self)]
        while stack:
            depth, node = stack.pop()
            yield (depth, node) if with_depth else node
            for child in reversed(node.children):
                stack.append((depth + 1, child))

    def find(self, kind):
        """First node of this kind in pre-order, or None."""
        for node in self.walk():
            if node.kind == kind:
                return node
        return None

    def text_leaves(self):
        """Every piece of text in the tree, in pre-order: string/text values and the str
        labels (keys) of dictionary entries. Field names of archived objects and type codes
        are structure, not content, and are left out."""
        for node in self.walk():
            if node.kind == "dict":
                for child in node.children:
                    if isinstance(child.label, str) and child.label:
                        yield child.label
            if node.kind in TEXT_KINDS and isinstance(node.value, str) and node.value:
                yield node.value

    def to_plain(self):
        """A JSON-serialisable rendering of the tree.

        Containers become dicts/lists (a repeated key gets a "#index" suffix), scalars stay
        scalars (a string keeps its text even when it has a nested decode), dates are ISO
        strings, UIDs are {"$uid": n}, objects carry "$class" and images "$image". A "bytes"
        node renders as its best (first) interpretation when it has one, otherwise as
        {"$bytes": hex, "$length": n} (hex capped at HEX_PREVIEW bytes). Format and transform
        nodes (bplist, gzip, json, ...) render as their content.
        """
        kind = self.kind
        if kind == "bytes":
            if self.children:
                return self.children[0].to_plain()
            return _bytes_plain(self.value)
        if kind in _KEYED_KINDS:
            out = {}
            if kind == "object":
                out["$class"] = self.value
            elif kind == "image":
                out["$image"] = self.value
            for i, child in enumerate(self.children):
                key = _plain_key(child.label if child.label is not None else i)
                if key in out:
                    key = "%s#%d" % (key, i)
                out[key] = child.to_plain()
            return out
        if kind == "protobuf":
            # Field number -> value; a repeated field becomes a list of its values.
            grouped = {}
            for child in self.children:
                grouped.setdefault(_plain_key(child.label), []).append(child.to_plain())
            return dict((k, v[0] if len(v) == 1 else v) for k, v in grouped.items())
        if kind == "array":
            return [child.to_plain() for child in self.children]
        if kind == "uid":
            return {"$uid": self.value}
        if kind == "null":
            return None
        if kind == "json" and len(self.children) == 1 and self.children[0].kind == "string"                 and any(c.kind == "json" for c in self.children[0].children):
            # JSON saved as a JSON string: the JSON inside it
            inner = next(c for c in self.children[0].children if c.kind == "json")
            return inner.to_plain()
        if self.children and kind not in _SCALAR_KINDS:
            # Format and transform nodes wrap their content.
            if len(self.children) == 1:
                return self.children[0].to_plain()
            return [child.to_plain() for child in self.children]
        if isinstance(self.value, (bytes, bytearray)):
            return _bytes_plain(self.value)
        if isinstance(self.value, float) and self.value != self.value:
            return None     # NaN is not JSON
        return self.value


_KEYED_KINDS = frozenset(("dict", "object", "nskeyedarchive", "image"))
_SCALAR_KINDS = frozenset(("string", "text", "int", "float", "bool", "date", "uuid", "file"))


def _plain_key(label):
    if isinstance(label, str):
        return label
    return repr(label) if label is not None else "null"


def _bytes_plain(data):
    data = bytes(data or b"")
    out = {"$bytes": binascii.hexlify(data[:HEX_PREVIEW]).decode("ascii"), "$length": len(data)}
    if len(data) > HEX_PREVIEW:
        out["$truncated"] = True
    return out


class Attempt(tuple):
    """One interpretation attempt: a (kind, confidence, node, reason) tuple.

    confidence is "confident", "uncertain" or "failed" (node is None when failed).
    `primary` is False for views that only the inspector should list (e.g. the raw,
    unresolved form of an NSKeyedArchiver plist); decode_blob() never picks those.
    """

    def __new__(cls, kind, confidence, node, reason, primary=True):
        self = tuple.__new__(cls, (kind, confidence, node, reason))
        self.primary = primary
        return self

    kind = property(lambda self: self[0])
    confidence = property(lambda self: self[1])
    node = property(lambda self: self[2])
    reason = property(lambda self: self[3])


def failed(kind, reason):
    return Attempt(kind, FAILED, None, reason)
