"""Text family: UTF-8 and UTF-16 text, JSON, base64 and 16-byte UUIDs.

Text is "confident" when it decodes strictly and at most 1% of its characters are control
characters (tab, CR and LF are fine; trailing NUL terminators are ignored). Base64 is only
confident when the decoded bytes themselves decode to something confident, so ordinary words
and hex strings are not reported as base64.
"""

import base64
import binascii
import json
import re
import uuid

from . import detect
from .nodes import CONFIDENT, FAILED, UNCERTAIN, Attempt, failed

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SAMPLE = 1 << 16           # characters examined at each end of long text
CLEAN, MOSTLY = 0.01, 0.10  # control-character ratios for confident / uncertain text
_JSON_START = frozenset('{["-0123456789tfn')


class _Pairs(list):
    """A JSON object as (key, value) pairs: keeps duplicate keys, which dict would drop."""


def control_ratio(s):
    """Share of control characters, estimated from both ends of long strings."""
    if len(s) > 2 * _SAMPLE:
        s = s[:_SAMPLE] + s[-_SAMPLE:]
    if not s:
        return 0.0
    return len(_CONTROL.findall(s)) / float(len(s))


def text_confidence(s):
    ratio = control_ratio(s)
    if ratio <= CLEAN:
        return CONFIDENT
    if ratio <= MOSTLY:
        return UNCERTAIN
    return FAILED


def is_clean_text(s):
    return bool(s) and control_ratio(s) <= CLEAN


def _strip_terminators(data, width):
    """Drop trailing NUL terminators (C strings) of `width` bytes each, keeping at least one
    unit; returns (body, count). Counts with rstrip: one copy, even for huge NUL runs."""
    zeros = len(data) - len(data.rstrip(b"\x00"))
    units = min(zeros // width, len(data) // width - 1)
    if units <= 0:
        return data, 0
    return data[:len(data) - units * width], units


def decode(data, ctx, depth, skip=()):
    """Attempts for the text family, in the order text, JSON, base64, UTF-16."""
    attempts = []
    utf8_text = None
    body = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    body, nuls = _strip_terminators(body, 1)
    try:
        s = body.decode("utf-8")
    except UnicodeDecodeError as e:
        s = None
        if "text" not in skip:
            attempts.append(failed("text", "not valid UTF-8 (byte %d)" % e.start))
    if s is not None:
        if "text" not in skip:
            utf8_text = _text_attempt(s, data, ctx, "UTF-8", nuls)
            attempts.append(utf8_text)
        if "json" not in skip:
            a = _json_attempt(s, data, ctx, depth)
            if a is not None:
                attempts.append(a)
        if "base64" not in skip:
            a = _base64_attempt(body, data, ctx, depth)
            if a is not None:
                attempts.append(a)
    # UTF-16 of ASCII is valid UTF-8 full of NULs, so it is tried whenever UTF-8 did not
    # give clean text.
    if "text" not in skip and (utf8_text is None or utf8_text.confidence != CONFIDENT):
        a = _utf16_attempt(data, ctx)
        if a is not None:
            attempts.append(a)
    return attempts


def _text_attempt(s, data, ctx, encoding, nuls, note=""):
    conf = text_confidence(s)
    if conf == FAILED:
        return failed("text", "%s but mostly control characters" % encoding)
    notes = [encoding]
    if note:
        notes.append(note)
    if nuls:
        notes.append("NUL-terminated")
    node = ctx.node("text", value=s, offset=0, length=len(data), confidence=conf,
                    note=", ".join(notes))
    return Attempt("text", conf, node, "%s text, %d characters" % (encoding, len(s)))


# -- UTF-16 -------------------------------------------------------------------
def _utf16_attempt(data, ctx):
    if len(data) < 4:
        return None
    note = ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "UTF-16LE" if data[:2] == b"\xff\xfe" else "UTF-16BE"
        body, note = data[2:], "BOM"
    else:
        sample = data[:4096]
        half = len(sample) // 2
        odd_zero = sample[1::2].count(0) / float(half)
        even_zero = sample[0::2].count(0) / float(half)
        if odd_zero >= 0.8 and even_zero <= 0.2:
            encoding = "UTF-16LE"
        elif even_zero >= 0.8 and odd_zero <= 0.2:
            encoding = "UTF-16BE"
        else:
            return None
        body = data
    if len(body) % 2 and body.endswith(b"\x00"):
        body = body[:-1]
    body, nuls = _strip_terminators(body, 2)
    try:
        s = body.decode("utf-16-le" if encoding == "UTF-16LE" else "utf-16-be")
    except UnicodeDecodeError as e:
        return failed("text", "not valid %s (byte %d)" % (encoding, e.start))
    return _text_attempt(s, data, ctx, encoding, nuls, note)


# -- JSON ---------------------------------------------------------------------
def _loads(s):
    return json.loads(s, object_pairs_hook=_Pairs)


def _json_attempt(s, data, ctx, depth):
    head = s.lstrip()[:1]
    if head not in _JSON_START:
        return None
    try:
        value = _loads(s)
    except (ValueError, RecursionError) as e:
        return failed("json", "not JSON: %s" % str(e)[:80])
    top = json_node(value, None, ctx, depth)
    structured = isinstance(value, list)        # _Pairs is a list too
    note = _shape(value)
    if not structured and isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        # JSON saved as a JSON string ("{\"a\":1}"): as structured as the JSON inside it
        try:
            inner = _loads(value)
        except (ValueError, RecursionError):
            inner = None
        if isinstance(inner, list):
            structured = True
            note = "string holding JSON %s" % _shape(inner)
    conf = CONFIDENT if structured else UNCERTAIN
    node = ctx.node("json", offset=0, length=len(data), children=[top], confidence=conf,
                    note=note)
    reason = "JSON " + note if structured else "whole value is a JSON scalar"
    return Attempt("json", conf, node, reason)


def _shape(value):
    if isinstance(value, _Pairs):
        return "object (%d keys)" % len(value)
    if isinstance(value, list):
        return "array (%d items)" % len(value)
    return "scalar"


def json_node(value, label, ctx, depth):
    """Node tree for a parsed JSON value (strings may carry nested decodes)."""
    if isinstance(value, list):
        is_object = isinstance(value, _Pairs)
        node = ctx.node("dict" if is_object else "array", label)
        if not ctx.can_nest():
            node.note = "nesting limit reached"
            return node
        ctx.nest += 1
        try:
            for i, item in enumerate(value):
                if ctx.full():
                    node.note = "node limit reached after %d entries" % i
                    break
                key, item = item if is_object else (i, item)
                node.children.append(json_node(item, key, ctx, depth))
        finally:
            ctx.nest -= 1
        return node
    if isinstance(value, str):
        return ctx.string_node(value, depth, label)
    if isinstance(value, bool):
        return ctx.node("bool", label, value)
    if isinstance(value, int):
        return ctx.node("int", label, value)
    if isinstance(value, float):
        return ctx.node("float", label, value)
    return ctx.node("null", label)


# -- base64 -------------------------------------------------------------------
def _b64decode(compact, urlsafe):
    if urlsafe:
        return base64.urlsafe_b64decode(compact)
    return base64.b64decode(compact, validate=True)


def meaningful(bytes_node):
    """True if a decoded "bytes" node has a confident interpretation."""
    return any(c.confidence == CONFIDENT for c in bytes_node.children)


def _base64_attempt(body, data, ctx, depth):
    compact, urlsafe = detect.looks_base64(body)
    if compact is None:
        return None
    try:
        raw = _b64decode(compact, urlsafe)
    except (binascii.Error, ValueError):
        return failed("base64", "bad base64 padding")
    child = ctx.bytes_node(raw, depth + 1)
    conf = CONFIDENT if meaningful(child) else UNCERTAIN
    node = ctx.node("base64", offset=0, length=len(data), children=[child], confidence=conf,
                    note="URL-safe alphabet" if urlsafe else "")
    reason = "%d bytes of base64 decode to %d bytes" % (len(compact), len(raw))
    if conf == UNCERTAIN:
        reason += " with no recognised content"
    return Attempt("base64", conf, node, reason)


def decode_inner_string(s, ctx, depth):
    """A JSON or base64 decode of a string found inside another structure, or None.

    Only structured JSON (object/array) and base64 whose payload decodes confidently count,
    so ordinary strings stay plain."""
    head = s.lstrip()[:1]
    if head in ("{", "["):
        try:
            value = _loads(s)
        except (ValueError, RecursionError):
            return None
        if isinstance(value, list):
            top = json_node(value, None, ctx, depth)
            return ctx.node("json", children=[top], note=_shape(value))
        return None
    try:
        raw_text = s.encode("ascii")
    except UnicodeEncodeError:
        return None
    compact, urlsafe = detect.looks_base64(raw_text)
    if compact is None:
        return None
    try:
        raw = _b64decode(compact, urlsafe)
    except (binascii.Error, ValueError):
        return None
    child = ctx.bytes_node(raw, depth + 1)
    if not meaningful(child):
        return None
    return ctx.node("base64", children=[child], note="URL-safe alphabet" if urlsafe else "")


# -- UUID -----------------------------------------------------------------------
def decode_uuid(data, ctx):
    """16 bytes that carry a valid RFC 4122 version and variant, in either byte order."""
    readings = []
    for byte_order, u in (("big-endian", uuid.UUID(bytes=bytes(data))),
                          ("little-endian GUID", uuid.UUID(bytes_le=bytes(data)))):
        if u.variant == uuid.RFC_4122 and 1 <= (u.version or 0) <= 8:
            readings.append((byte_order, u))
    if not readings:
        return None
    byte_order, u = readings[0]
    note = "%s, version %d" % (byte_order, u.version)
    if len(readings) > 1:
        note += "; little-endian GUID reading: %s" % readings[1][1]
    node = ctx.node("uuid", value=str(u), offset=0, length=16, confidence=UNCERTAIN, note=note)
    return Attempt("uuid", UNCERTAIN, node, "16 bytes with a valid UUID version and variant")
