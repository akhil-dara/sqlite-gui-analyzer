"""Decoded values as documents: JSON, an XML property list (for plists only) or protobuf
fields in the `protoc --decode_raw` layout, and the format a node belongs to, so each BLOB
is shown and saved in the form that suits it (a protobuf is never dressed up as a plist).
"""

import binascii
import datetime
import json

from . import decode_blob
from .describe import TRANSFORMS

PLIST_KINDS = frozenset(("bplist", "xml_plist"))
FORMAT_KINDS = {"bplist": "plist", "xml_plist": "plist", "protobuf": "protobuf",
                "json": "json", "typedstream": "typedstream"}
BYTES_SHOWN = 64                # bytes written out in full in the protobuf view


def format_of(path):
    """'plist', 'protobuf', 'json', 'typedstream', 'text' or '' for the node at the end of
    path (root first): the nearest enclosing format decides."""
    for node in reversed(path):
        if node.kind in FORMAT_KINDS:
            return FORMAT_KINDS[node.kind]
        if node.kind in ("text", "string"):
            return "text"
    # a compression or base64 layer: the format of what it holds (gzip → bplist is a plist)
    node = path[-1] if path else None
    for _ in range(16):
        if node is None or not (node.kind in TRANSFORMS or node.kind == "bytes"):
            break
        node = node.children[0] if node.children else None
        if node is not None and node.kind in FORMAT_KINDS:
            return FORMAT_KINDS[node.kind]
        if node is not None and node.kind == "text":
            return "text"
    return ""


def _plist_safe(v):
    """A decoded value as plistlib can write it: None as an empty string, dict keys as text,
    other objects as their text."""
    if v is None:
        return ""
    if isinstance(v, bool) or isinstance(v, (str, bytes, float, datetime.datetime)):
        return v
    if isinstance(v, int):
        return v if -2 ** 63 <= v < 2 ** 64 else str(v)
    if isinstance(v, dict):
        return dict((str(k), _plist_safe(x)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return [_plist_safe(x) for x in v]
    if isinstance(v, (bytearray, memoryview)):
        return bytes(v)
    return str(v)


def plist_xml(value):
    """A decoded value as an XML property list (the form Apple's tools show)."""
    import plistlib
    return plistlib.dumps(_plist_safe(value), fmt=plistlib.FMT_XML,
                          sort_keys=False).decode("utf-8")


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _quote(s):
    return json.dumps(s, ensure_ascii=False)


def protobuf_text(node, indent=0):
    """The fields of a protobuf node as `protoc --decode_raw` prints them: `1: 150`,
    `2: "text"`, nested messages in braces; other bytes as hex (the first BYTES_SHOWN)."""
    pad = "  " * indent
    out = []
    for f in node.children:
        label = f.label if f.label is not None else "?"
        inner = _message_of(f)
        if inner is not None:
            out.append("%s%s {" % (pad, label))
            out.append(protobuf_text(inner, indent + 1))
            out.append("%s}" % pad)
        elif f.kind in ("string", "text"):
            out.append("%s%s: %s" % (pad, label, _quote(f.value or "")))
        elif f.kind == "bytes":
            data = bytes(f.value or b"")
            hexed = binascii.hexlify(data[:BYTES_SHOWN]).decode("ascii")
            more = "… (%d bytes)" % len(data) if len(data) > BYTES_SHOWN else ""
            out.append("%s%s: <%s%s>" % (pad, label, hexed, more))
        else:
            out.append("%s%s: %s" % (pad, label, f.value))
    return "\n".join(line for line in out if line)


def _message_of(field):
    """The nested message of a field (itself, or the first reading of its bytes)."""
    if field.kind == "protobuf":
        return field
    if field.kind == "bytes" and field.children and field.children[0].kind == "protobuf":
        return field.children[0]
    return None


def first_format(root):
    """The format node a whole-BLOB decode leads to, through compression and base64
    (gzip → bplist gives the bplist node), or None."""
    node = root.children[0] if root.children else None
    for _ in range(16):
        if node is None:
            return None
        if node.kind in FORMAT_KINDS:
            return node
        if node.kind in TRANSFORMS or node.kind == "bytes":
            node = node.children[0] if node.children else None
            continue
        return None
    return None


VIEW_TITLES = {"xml": "XML plist", "protobuf": "protobuf fields", "json": "JSON"}
_PLAIN_KINDS = frozenset(("text", "string", "image", "file", "uuid", "date", "int", "float"))


def decoded_view(data):
    """(view title, text) of a BLOB's decoded value in the form that suits its format: an
    XML property list for plists, protobuf fields for protobuf, JSON otherwise; None when
    nothing decodes it or its one-line summary already says it all (text, images, dates)."""
    root = decode_blob(data)
    best = root.children[0] if root.children else None
    if best is None or best.kind == "bytes" or best.kind in _PLAIN_KINDS:
        return None
    node = first_format(root)
    if node is not None and node.kind in PLIST_KINDS:
        return VIEW_TITLES["xml"], plist_xml(node.to_plain())
    if node is not None and node.kind == "protobuf":
        return VIEW_TITLES["protobuf"], protobuf_text(node)
    return VIEW_TITLES["json"], json_text(root.to_plain())


KIND_NAMES = (("plist", "plist", "plists"), ("protobuf", "protobuf", "protobuf"),
              ("json", "JSON", "JSON"), ("other", "other decoded", "other decoded"),
              ("raw", "not decoded", "not decoded"))
SNIFF_BYTES = 256 << 10         # larger BLOBs are counted by their signature only


def blob_kind(data):
    """'plist', 'protobuf', 'json', 'other' (decoded some other way) or 'raw' (nothing
    decodes it): what a BLOB would give when saved decoded."""
    if data[:6] == b"bplist" or data.lstrip()[:5] in (b"<?xml", b"<plis"):
        return "plist"
    if len(data) > SNIFF_BYTES:
        return "raw"
    root = decode_blob(data)
    best = root.children[0] if root.children else None
    if best is None or best.kind == "bytes":
        return "raw"
    node = first_format(root)
    if node is not None:
        k = FORMAT_KINDS.get(node.kind, "other")
        return k if k in ("plist", "protobuf", "json") else "other"
    return "other"


def kinds_text(counts):
    """'120 plists, 30 protobuf, 4 not decoded' from {kind: count}."""
    parts = []
    for key, one, many in KIND_NAMES:
        n = counts.get(key, 0)
        if n:
            parts.append("%s %s" % (format(n, ","), one if n == 1 else many))
    return ", ".join(parts) or "no BLOB"


def decoded_file(data, mode):
    """(content bytes, extension) of a BLOB saved decoded, or (None, why not).

    mode 'json': any BLOB with a decoded reading, as JSON; 'xml': plists only (also inside
    compression), as an XML property list."""
    root = decode_blob(data)
    best = root.children[0] if root.children else None
    if best is None or best.kind == "bytes":
        return None, "not decoded"
    if mode == "xml":
        node = first_format(root)
        if node is None or node.kind not in PLIST_KINDS:
            return None, "not a plist"
        return plist_xml(node.to_plain()).encode("utf-8"), ".xml.plist"
    return json_text(root.to_plain()).encode("utf-8"), ".json"
