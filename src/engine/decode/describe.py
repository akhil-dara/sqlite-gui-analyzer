"""One-line human descriptions of decoded trees (for grid cells and status lines).

Examples: "bplist: NSKeyedArchiver NSDictionary (12 keys)", "gzip → protobuf (5 fields)",
"PNG 640×480", "UTF-16LE text: 'Hello…'". An uncertain interpretation is marked
with "?" after its kind ("protobuf? (3 fields)").
"""

from .nodes import CONFIDENT

TRANSFORMS = {"gzip": "gzip", "zlib": "zlib", "deflate": "deflate", "bz2": "bzip2",
              "xz": "xz", "lzma": "lzma", "zstd": "zstd", "base64": "base64",
              "lz4": "lz4", "lz4_apple": "Apple LZ4", "lzfse": "lzfse", "lzvn": "lzvn"}
ARROW = " → "
QUOTE_CHARS = 40


def quote(text, limit=QUOTE_CHARS):
    """'text' with whitespace runs folded and an ellipsis past `limit` characters."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return "'%s'" % flat


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def value_shape(node):
    """Short description of a decoded value node."""
    kind = node.kind
    if kind == "dict":
        return "%s (%s)" % (node.value or "dict", _count(len(node.children), "key"))
    if kind == "array":
        return "%s (%s)" % (node.value or "array", _count(len(node.children), "item"))
    if kind == "object":
        return "%s object" % (node.value or "?")
    if kind in ("string", "text"):
        return "string " + quote(node.value or "")
    if kind == "bytes":
        if node.children:
            return describe_chain(node)
        return "data (%s)" % _count(len(node.value or b""), "byte")
    if kind == "null":
        return "null"
    if kind == "uid":
        return "UID %s" % node.value
    return "%s %s" % (kind, node.value)


def _mark(node, label):
    return label if node.confidence == CONFIDENT else label + "?"


def _describe_one(node):
    kind = node.kind
    if kind in ("bplist", "xml_plist"):
        label = _mark(node, "bplist" if kind == "bplist" else "XML plist")
        if not node.children:
            return label
        inner = node.children[0]
        if inner.kind == "nskeyedarchive":
            top = _archive_root(inner)
            shown = value_shape(top) if top is not None else "(empty)"
            return "%s: %s %s" % (label, inner.value or "NSKeyedArchiver", shown)
        return "%s: %s" % (label, value_shape(inner))
    if kind == "json":
        top = node.children[0] if node.children else None
        if top is None:
            return _mark(node, "JSON")
        if top.kind == "string":
            inner = next((c for c in top.children if c.kind == "json"), None)
            if inner is not None and inner.children:
                top = inner.children[0]         # JSON saved as a JSON string
        if top.kind in ("dict", "array"):
            noun = "object" if top.kind == "dict" else "array"
            unit = "key" if top.kind == "dict" else "item"
            return "%s %s (%s)" % (_mark(node, "JSON"), noun, _count(len(top.children), unit))
        return "%s %s" % (_mark(node, "JSON"), value_shape(top))
    if kind == "protobuf":
        return "%s (%s)" % (_mark(node, "protobuf"), _count(len(node.children), "field"))
    if kind == "text":
        encoding = (node.note or "text").split(",")[0]
        return "%s %s: %s" % (encoding, _mark(node, "text"), quote(node.value or ""))
    if kind == "typedstream":
        top = next((c for c in node.children if c.kind in ("object", "string", "dict")), None)
        name = (top.value if top is not None and top.kind != "string" else None) or ""
        head = _mark(node, "typedstream") + (" " + name if name else "")
        return head + (": " + quote(node.value) if node.value else "")
    if kind == "image":
        return _mark(node, node.value or "image") + (" " + node.note if node.note else "")
    if kind == "file":
        return node.value or "file"
    if kind == "uuid":
        return "UUID? %s" % node.value
    if kind == "date":
        return "%s %s" % (_mark(node, "timestamp"), node.value)
    if kind in TRANSFORMS:
        return "%s (%s)" % (_mark(node, TRANSFORMS[kind]), node.note) if node.note \
            else _mark(node, TRANSFORMS[kind])
    return value_shape(node)


def _archive_root(archive):
    for child in archive.children:
        if child.label == "root":
            return child
    return archive.children[0] if archive.children else None


def describe_chain(bytes_node, max_steps=8):
    """Follow the best interpretation through transforms: "gzip → protobuf (5 fields)"."""
    parts = []
    node = bytes_node
    for _ in range(max_steps):
        if not node.children:
            break
        best = node.children[0]
        if best.kind in TRANSFORMS and best.children and best.children[0].kind == "bytes":
            parts.append(_mark(best, TRANSFORMS[best.kind]))
            inner = best.children[0]
            if not inner.children:
                parts.append("data (%s)" % _count(len(inner.value or b""), "byte"))
                break
            node = inner
            continue
        parts.append(_describe_one(best))
        break
    return ARROW.join(parts)


def summarize(root):
    """One line for a decode_blob() tree."""
    if root.kind != "bytes":
        return value_shape(root)
    size = len(root.value or b"")
    if size == 0:
        return "empty"
    line = describe_chain(root)
    return line or "binary data (%s)" % _count(size, "byte")
