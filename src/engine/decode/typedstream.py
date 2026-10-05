"""NeXTSTEP/Apple typedstream (NSArchiver) decoding, e.g. sms.db message.attributedBody.

Stream layout (all integers little-endian):
  header   0x04 (version) 0x0b "streamtyped" <int system version, usually 1000>
  body     a sequence of typed groups: <type encoding> <one value per type in it>

Integers use a tagged form: a signed byte is the value itself unless it is a tag:
0x81 = int16 follows, 0x82 = int32 follows, 0x83 = IEEE float/double follows (reals only),
0x84 = new entry, 0x85 = nil, 0x86 = end of object. Bytes 0x92 and above (and values read
via 0x81/0x82) are references: index = value + 110 (0x92 is index 0).

Two tables are built while reading: shared strings (type encodings, class names, C-string
contents) and objects (objects, classes and C strings, in the order they appear). A new object
is 0x84, its class chain (0x84 <name> <version> ... ending with nil or a class reference),
then typed groups until 0x86. Type "+" is a length-prefixed byte string (NSString contents);
"*" is a C string: 0x84 followed by a shared string.

Result: typedstream -> values, objects as "object" nodes (value = class name) except
NSString family -> "string", NSNumber -> "int"/"float", NSDictionary -> "dict",
NSArray/NSSet -> "array", NSData -> "bytes" (decoded again). The root's value is the first
string (the message text of an attributedBody).
"""

import struct

from .nodes import CONFIDENT, UNCERTAIN, Attempt, failed

MAGIC = b"\x04\x0bstreamtyped"
TAG_INT16, TAG_INT32, TAG_REAL, TAG_NEW, TAG_NIL, TAG_END = 0x81, 0x82, 0x83, 0x84, 0x85, 0x86
REF_BASE = -110                 # 0x92 as a signed byte: reference number 0
MAX_CLASS_CHAIN = 64
MAX_ARRAY = 1 << 20             # the default of limits 'typedstream_max_array'

_INT_TYPES = {"c": 8, "C": 8, "s": 16, "S": 16, "i": 32, "I": 32, "l": 32, "L": 32,
              "q": 64, "Q": 64}
_UNSIGNED = frozenset("CSILQ")
_QUALIFIERS = frozenset("rnNoORV")
_STRING_CLASSES = frozenset(("NSString", "NSMutableString"))
_DICT_CLASSES = frozenset(("NSDictionary", "NSMutableDictionary"))
_ARRAY_CLASSES = frozenset(("NSArray", "NSMutableArray", "NSSet", "NSMutableSet",
                            "NSOrderedSet", "NSMutableOrderedSet"))
_DATA_CLASSES = frozenset(("NSData", "NSMutableData"))


class StreamError(Exception):
    pass


class _Class(object):
    __slots__ = ("name", "version", "superclass")

    def __init__(self, name, version):
        self.name, self.version, self.superclass = name, version, None

    def chain(self):
        names, cls = [], self
        while cls is not None and len(names) < MAX_CLASS_CHAIN:
            names.append(cls.name)
            cls = cls.superclass
        return names


def split_types(encoding):
    """Top-level type codes of an encoding: "iI" -> ["i", "I"], "{P=dd}c" -> ["{P=dd}", "c"]."""
    out, i, n = [], 0, len(encoding)
    while i < n:
        ch = encoding[i]
        if ch in _QUALIFIERS:
            i += 1
            continue
        if ch in "{[(":
            close = {"{": "}", "[": "]", "(": ")"}[ch]
            level, j = 0, i
            while j < n:
                if encoding[j] == ch:
                    level += 1
                elif encoding[j] == close:
                    level -= 1
                    if level == 0:
                        break
                j += 1
            if j >= n:
                raise StreamError("unbalanced type encoding %r" % encoding)
            out.append(encoding[i:j + 1])
            i = j + 1
        elif ch == "^":
            raise StreamError("pointer types are not supported")
        else:
            out.append(ch)
            i += 1
    return out


class _Reader(object):
    def __init__(self, data, ctx, depth):
        self.data, self.ctx, self.depth = data, ctx, depth
        self.pos = 0
        self.strings = []           # shared strings (bytes)
        self.objects = []           # _Class, finished Node, or None while being read

    # -- primitives ---------------------------------------------------------------
    def byte(self):
        if self.pos >= len(self.data):
            raise StreamError("unexpected end of stream at offset %d" % self.pos)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise StreamError("%d bytes at offset %d run past the end" % (n, self.pos))
        raw = self.data[self.pos:self.pos + n]
        self.pos += n
        return raw

    def int_after(self, first):
        """The integer whose first byte (already read) is `first`."""
        if first == TAG_INT16:
            return struct.unpack("<h", self.take(2))[0]
        if first == TAG_INT32:
            return struct.unpack("<i", self.take(4))[0]
        return first - 256 if first >= 128 else first

    def read_int(self):
        return self.int_after(self.byte())

    def reference(self, first, table, what):
        """Index into table of the reference whose first byte is `first`."""
        index = self.int_after(first) - REF_BASE
        if not 0 <= index < len(table):
            raise StreamError("bad %s reference %d at offset %d" % (what, index, self.pos))
        return index

    def shared_string(self):
        """A type encoding / class name / C-string body: new, nil or a reference."""
        b = self.byte()
        if b == TAG_NIL:
            return None
        if b == TAG_NEW:
            raw = self.take(self.read_int())
            self.strings.append(raw)
            return raw
        return self.strings[self.reference(b, self.strings, "string")]

    # -- classes and objects ------------------------------------------------------
    def read_class(self):
        first = None
        cls_prev = None
        for _ in range(MAX_CLASS_CHAIN):
            b = self.byte()
            if b == TAG_NIL:
                return first
            if b == TAG_NEW:
                name = self.shared_string()
                cls = _Class(_text(name) if name is not None else "?", self.read_int())
                self.objects.append(cls)
            else:
                cls = self.objects[self.reference(b, self.objects, "class")]
                if not isinstance(cls, _Class):
                    raise StreamError("class reference to a non-class at offset %d" % self.pos)
            if cls_prev is not None:
                cls_prev.superclass = cls
            first = first or cls
            if b != TAG_NEW:
                return first        # a referenced class already carries its superclasses
            cls_prev = cls
        raise StreamError("class chain too long")

    # Every reader appends its node to `parent` as soon as it exists, so a stream that
    # breaks off still leaves everything decoded before the break in the tree.
    def read_object(self, label, parent):
        ctx = self.ctx
        start = self.pos
        b = self.byte()
        if b == TAG_NIL:
            return _emit(parent, ctx.node("null", label, offset=start, length=1))
        if b != TAG_NEW:
            index = self.reference(b, self.objects, "object")
            return _emit(parent, self._reference_node(index, label, start))
        slot = len(self.objects)
        self.objects.append(None)
        cls = self.read_class()
        name = cls.name if cls is not None else "?"
        node = _emit(parent, ctx.node("object", label, name, offset=start))
        position = len(parent.children) - 1
        chain = cls.chain() if cls is not None else []
        if len(chain) > 1:
            node.note = " : ".join(chain)
        if not ctx.can_nest():
            raise StreamError("objects nested too deeply at offset %d" % start)
        ctx.nest += 1
        try:
            while True:
                if self.pos < len(self.data) and self.data[self.pos] == TAG_END:
                    self.pos += 1
                    break
                self.read_group(node)
        finally:
            ctx.nest -= 1
            node.length = self.pos - start
        node = _finish(node, chain, ctx, self.depth)
        parent.children[position] = node
        self.objects[slot] = node
        return node

    def _reference_node(self, index, label, start):
        ctx = self.ctx
        target = self.objects[index]
        length = self.pos - start
        if target is None:
            return ctx.node("uid", label, offset=start, length=length,
                            note="reference to an object still being decoded (cycle)")
        if isinstance(target, _Class):
            return ctx.node("object", label, target.name, offset=start, length=length,
                            note="class reference")
        if target.kind in ("string", "int", "float", "null", "bool", "date"):
            return ctx.node(target.kind, label, target.value, offset=start, length=length,
                            note=_join(target.note, "repeat of object #%d" % index))
        return ctx.node("uid", label, index, offset=start, length=length,
                        note="reference to earlier %s object #%d" % (target.value or target.kind,
                                                                      index))

    # -- values -------------------------------------------------------------------
    def read_group(self, parent):
        if not self.ctx.step():
            raise StreamError("decode work limit reached at offset %d" % self.pos)
        encoding = self.shared_string()
        if encoding is None:
            raise StreamError("nil type encoding at offset %d" % self.pos)
        encoding = _text(encoding)
        types = split_types(encoding)
        if len(types) == 1:
            self.read_value(types[0], types[0], parent)
            return
        group = _emit(parent, self.ctx.node("array", encoding, offset=self.pos,
                                            note="type group"))
        try:
            for code in types:
                self.read_value(code, code, group)
        finally:
            group.length = self.pos - group.offset

    def read_value(self, code, label, parent):
        ctx = self.ctx
        start = self.pos
        head = code[0]
        if head == "@":
            return self.read_object(label, parent)
        if head == "[":
            return self._array(code, label, start, parent)
        if head == "{":
            return self._struct(code, label, start, parent)
        if head in _INT_TYPES:
            value = self.read_int()
            if head in _UNSIGNED and value < 0:
                value += 1 << _INT_TYPES[head]
            node = ctx.node("int", label, value, offset=start)
        elif head in ("f", "d"):
            b = self.byte()
            if b == TAG_REAL:
                size = 4 if head == "f" else 8
                value = struct.unpack("<f" if size == 4 else "<d", self.take(size))[0]
            else:
                value = float(self.int_after(b))
            node = ctx.node("float", label, value, offset=start)
        elif head == "+":
            raw = self.take(self.read_int())
            return _emit(parent, _string_or_bytes(ctx, self.depth, raw, label,
                                                  self.pos - len(raw)))
        elif head == "*":
            b = self.byte()
            if b == TAG_NIL:
                node = ctx.node("null", label, offset=start)
            elif b == TAG_NEW:
                raw = self.shared_string()
                node = ctx.node("string", label, _text(raw or b""), offset=start,
                                note="C string")
                self.objects.append(node)
            else:
                node = self._reference_node(self.reference(b, self.objects, "C string"),
                                            label, start)
        elif head in (":", "%"):
            raw = self.shared_string()
            node = ctx.node("string", label, _text(raw or b""), offset=start,
                            note="selector" if head == ":" else "atom")
        elif head == "#":
            cls = self.read_class()
            node = ctx.node("object", label, cls.name if cls else None, offset=start,
                            note="class")
        else:
            raise StreamError("unsupported type %r at offset %d" % (code, start))
        node.length = self.pos - start
        return _emit(parent, node)

    def _array(self, code, label, start, parent):
        inner = code[1:-1]
        digits = 0
        while digits < len(inner) and inner[digits].isdigit():
            digits += 1
        if not digits:
            raise StreamError("array type without a count: %r" % code)
        count, element = int(inner[:digits]), inner[digits:]
        if count > self.ctx.typedstream_array:
            raise StreamError("array of %d elements is implausible" % count)
        if element in ("c", "C"):
            raw = self.take(count)
            return _emit(parent, self.ctx.bytes_node(raw, self.depth + 1, label, offset=start,
                                                     length=count))
        node = _emit(parent, self.ctx.node("array", label, offset=start, note=code))
        types = split_types(element)
        try:
            for i in range(count):
                if not self.ctx.step():
                    raise StreamError("decode work limit reached at offset %d" % self.pos)
                for t in types:
                    self.read_value(t, i, node)
        finally:
            node.length = self.pos - start
        return node

    def _struct(self, code, label, start, parent):
        body = code[1:-1]
        name, _, fields = body.partition("=")
        node = _emit(parent, self.ctx.node("object", label, name or "struct", offset=start,
                                           note="struct"))
        try:
            for t in split_types(fields):
                self.read_value(t, t, node)
        finally:
            node.length = self.pos - start
        return node


def _emit(parent, node):
    parent.children.append(node)
    return node


def _text(raw):
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw).decode("latin-1")


def _join(a, b):
    return "%s; %s" % (a, b) if a and b else (a or b or "")


def _string_or_bytes(ctx, depth, raw, label, offset):
    try:
        value = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return ctx.bytes_node(raw, depth + 1, label, offset=offset, length=len(raw),
                              note="byte string (not UTF-8)")
    return ctx.string_node(value, depth, label, offset=offset, length=len(raw))


def _flat_values(node):
    """Values of an object's groups, with multi-value groups flattened."""
    out = []
    for child in node.children:
        if child.kind == "array" and child.note == "type group":
            out.extend(child.children)
        else:
            out.append(child)
    return out


def _finish(node, chain, ctx, depth):
    """Turn well-known Foundation objects into plain nodes (keeping offsets and class)."""
    names = set(chain)
    values = _flat_values(node)
    replacement = None
    if names & _STRING_CLASSES and len(values) == 1 and values[0].kind == "string":
        replacement = values[0]
    elif names & {"NSNumber", "NSValue"} and len(values) == 2 and values[0].kind == "string" \
            and values[1].kind in ("int", "float"):
        replacement = values[1]
        replacement.note = _join(replacement.note, "objCType %s" % values[0].value)
    elif names & _DICT_CLASSES and values and values[0].kind == "int":
        replacement = ctx.node("dict", value=node.value)
        items = values[1:]
        for i in range(0, len(items) - 1, 2):
            key, value = items[i], items[i + 1]
            value.label = key.value if key.kind == "string" else "#%d" % (i // 2)
            replacement.children.append(value)
    elif names & _ARRAY_CLASSES and values and values[0].kind == "int":
        replacement = ctx.node("array", value=node.value)
        for i, item in enumerate(values[1:]):
            item.label = i
            replacement.children.append(item)
    elif names & _DATA_CLASSES:
        blobs = [v for v in values if v.kind == "bytes"]
        if len(blobs) == 1:
            replacement = blobs[0]
    if replacement is None:
        if "NSAttributedString" in names or "NSMutableAttributedString" in names:
            for child in node.children:
                if child.kind == "string":
                    child.label = "string"
                    break
        return node
    replacement.label = node.label
    if replacement.kind in ("dict", "array"):
        replacement.note = node.note
    else:
        replacement.note = _join(node.value, replacement.note)
    if replacement.kind not in ("string", "bytes"):
        # Strings and data keep the position of their payload; the rest span the object.
        replacement.offset, replacement.length = node.offset, node.length
    return replacement


def decode(data, ctx, depth):
    """Attempt for a buffer starting with the typedstream signature."""
    reader = _Reader(data, ctx, depth)
    root = ctx.node("typedstream", offset=0, length=len(data))
    try:
        reader.take(len(MAGIC))
        system = reader.read_int()
        root.note = "typedstream v4, system version %d" % system
        while reader.pos < len(data):
            if ctx.full():
                raise StreamError("node limit reached at offset %d" % reader.pos)
            reader.read_group(root)
        problem = ""
    except (StreamError, RecursionError, struct.error) as e:
        problem = str(e) or type(e).__name__
    strings = [n for n in root.walk() if n.kind == "string" and n.value
               and n.note != "C string"]
    if strings:
        root.value = strings[0].value
    if problem:
        root.note = _join(root.note, "stopped: " + problem)
        if not strings:
            if not root.children:
                return failed("typedstream", problem)
            root.confidence = UNCERTAIN
            return Attempt("typedstream", UNCERTAIN, root, "partial decode; " + problem)
    count = len(strings)
    return Attempt("typedstream", CONFIDENT, root,
                   "typedstream with %d string%s" % (count, "" if count == 1 else "s"))
