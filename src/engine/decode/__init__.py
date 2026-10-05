"""BLOB decoding engine: recognise and recursively decode the binary formats found in
SQLite forensic data (standard library only).

Public API (none of these ever raises, and all are deterministic):

  decode_blob(data, max_depth=6) -> Node
      Root is always a "bytes" node (value = the buffer, offset 0). Its children are the
      interpretations chosen for it: every confident one, or, when none is confident, up to
      three uncertain ones (possibly none). Byte strings found inside a decode (NSData,
      protobuf bytes fields, decompressed output, base64 payloads, typedstream byte arrays)
      are "bytes" nodes decoded the same way, down to max_depth nested buffers.

  interpretations(data) -> [(kind, confidence, Node or None, reason), ...]
      Every interpretation tried for the top buffer, ranked confident > uncertain > failed,
      including ones decode_blob() hides (e.g. text under JSON, the raw form of an
      NSKeyedArchiver plist). Failed entries have node None and say why in reason.

  decoded_strings(data, max_depth=4, limit=10000) -> [str, ...]
      Distinct text found in the decoded tree (string/text values and dictionary keys), in
      tree order, for searching inside decoded content.

  summary(data) -> str
      A one-line description for grid cells, e.g. "gzip → protobuf (5 fields)".

Inputs: bytes-like values are decoded as is; str is decoded as its UTF-8 bytes; None and
numbers give a single scalar node. See nodes.Node for the tree shape and
timestamps for epoch conversions.
"""

from . import timestamps
from .core import Context, choose, rank, run
from .describe import summarize
from .nodes import CONFIDENT, FAILED, UNCERTAIN, Attempt, Node

__all__ = ["decode_blob", "interpretations", "decoded_strings", "summary", "Node",
           "timestamps", "CONFIDENT", "UNCERTAIN", "FAILED"]

MAX_DEPTH_LIMIT = 16        # the default of limits 'decode_max_depth'


def _summary_limits():
    """Grid cells call summary() for many rows at once, so it gets a smaller budget (the
    'summary_*' limits)."""
    from .. import limits
    return dict(max_depth=limits.get("summary_max_depth"), prefix="summary")


def _as_bytes(data):
    """bytes for bytes-like input or str (UTF-8), else None."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8", "surrogatepass")
    return None


def _scalar_root(data):
    if data is None:
        return Node("null")
    if isinstance(data, bool):
        return Node("bool", value=data)
    if isinstance(data, int):
        return Node("int", value=data)
    if isinstance(data, float):
        return Node("float", value=data)
    return Node("string", value=repr(data), note="not a byte string")


def _clamp_depth(max_depth, default):
    try:
        from .. import limits
        return max(0, min(limits.get("decode_max_depth"), int(max_depth)))
    except (TypeError, ValueError, OverflowError):
        return default


def _error_root(raw, err):
    return Node("bytes", value=raw, offset=0, length=len(raw or b""),
                note="decode error: %s: %s" % (type(err).__name__, str(err)[:100]))


def _decode(data, **limits):
    raw = _as_bytes(data)
    if raw is None:
        return _scalar_root(data)
    ctx = Context(**limits)
    try:
        root = ctx.bytes_node(raw, 0, offset=0)
    except Exception as e:      # noqa: BLE001 - public API must never raise
        return _error_root(raw, e)
    notes = []
    if isinstance(data, str):
        notes.append("text value, decoded as its UTF-8 bytes")
    if ctx.limits_hit:
        notes.append("decode limits reached: " + ctx.limits_text())
    if ctx.errors:
        notes.append("%d decoder error(s), first: %s" % (len(ctx.errors), ctx.errors[0]))
    root.note = "; ".join([root.note] + notes if root.note else notes)
    return root


def decode_blob(data, max_depth=6):
    """Decoded tree for data; see the module docstring for the shape."""
    return _decode(data, max_depth=_clamp_depth(max_depth, 6))


def interpretations(data):
    """All interpretations of data, ranked; see the module docstring."""
    raw = _as_bytes(data)
    if raw is None:
        return []
    try:
        ctx = Context(exhaustive=True)
        attempts = rank(run(raw, ctx, 0)) if raw else []
    except Exception as e:      # noqa: BLE001 - public API must never raise
        return [Attempt("bytes", FAILED, None, "decode error: %s: %s"
                        % (type(e).__name__, str(e)[:100]))]
    return attempts


def decoded_strings(data, max_depth=4, limit=10000):
    """Distinct strings in the decoded tree, in tree order."""
    try:
        root = decode_blob(data, max_depth)
        limit = int(limit)
        out, seen = [], set()
        for text in root.text_leaves():
            if text not in seen:
                seen.add(text)
                out.append(text)
                if len(out) >= limit:
                    break
        return out
    except Exception:           # noqa: BLE001 - public API must never raise
        return []


def summary(data):
    """One-line description of data for grid cells."""
    try:
        return summarize(_decode(data, **_summary_limits()))
    except Exception as e:      # noqa: BLE001 - public API must never raise
        return "undecodable (%s)" % type(e).__name__
