"""Schemaless protobuf wire-format parsing, strict enough to avoid most false positives.

Validity rules: the buffer must be consumed exactly; field numbers are 1..2^29-1 and not
19000-19999 (reserved); wire types 0, 1, 2, 5, plus groups (3/4) only when properly closed;
varints are at most 10 bytes, fit in 64 bits and are canonically encoded (a multi-byte varint
never ends in 0x00, which real encoders never emit).

Field nodes: label = field number; offset/length = the whole field (tag to end of value),
except "bytes" nodes, whose offset/length locate exactly the payload in `value` (note says
where the tag is). Kinds:
  int       varint (value: uint64, or int64 when the top bit is set); note: other readings
  int/float fixed64 / fixed32: float when the bits make a plausible IEEE value, else integer
  string    length-delimited valid UTF-8 text
  protobuf  length-delimited nested message (or a group, note "group")
  bytes     anything else, decoded again; "packed varints" alternative reading added as an
            uncertain "array" child when the payload parses that way
A length-delimited payload that is both valid text and a valid message is read as a message
when it starts with a byte below 0x20 (typical tag of fields 1-3), else as text. A payload
that parses as only one or two fields with a number above BIG_FIELD is kept as bytes: that is
what random IDs and hashes look like when they happen to parse.
The top-level result is "confident" only when strongly structured (see _Parser.structured).
Work is bounded by the Context's node, nesting and step budgets; when one runs out the rest
of the buffer is not validated and the result is at most "uncertain".
"""

import math
import struct

from . import text
from .nodes import CONFIDENT, UNCERTAIN, Attempt, failed

MAX_FIELD = (1 << 29) - 1
BIG_FIELD = 1000            # valid, but rare in real schemas: weighs against confidence
MAX_MESSAGE_NEST = 32            # the default of limits 'protobuf_max_nest'
MAX_PACKED = 4096                # the default of limits 'protobuf_max_packed'


class Invalid(Exception):
    """The bytes are not a valid protobuf message."""


def read_varint(buf, pos, end):
    """(value, next position) of the varint at pos; raises Invalid."""
    start, result, shift = pos, 0, 0
    while pos < end:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            if b == 0 and pos - start > 1:
                raise Invalid("non-canonical varint at offset %d" % start)
            if result >> 64:
                raise Invalid("varint overflows 64 bits at offset %d" % start)
            return result, pos
        shift += 7
        if shift >= 70:
            raise Invalid("varint longer than 10 bytes at offset %d" % start)
    raise Invalid("truncated varint at offset %d" % start)


def _signed(value, bits):
    return value - (1 << bits) if value >> (bits - 1) else value


def _plausible_float(f, limit):
    return f == 0.0 or (math.isfinite(f) and 1e-7 <= abs(f) <= limit)


class _Parser(object):
    def __init__(self, buf, ctx, depth):
        self.buf, self.ctx, self.depth = buf, ctx, depth
        self.fields = self.max_field = self.big_fields = self.texts = self.nested = 0
        self.stopped_at = None          # offset where a decode limit stopped parsing

    # Statistics and node counts of a nested attempt are rolled back if it fails.
    def _snapshot(self):
        return (self.fields, self.max_field, self.big_fields, self.texts, self.nested,
                self.ctx.nodes)

    def _restore(self, snap):
        (self.fields, self.max_field, self.big_fields, self.texts, self.nested,
         self.ctx.nodes) = snap

    def structured(self, size):
        """Enough structure to call it protobuf with confidence: several fields, nearly all
        with small numbers (<= BIG_FIELD), and at least one nested message or real string."""
        return (size >= 8 and self.fields >= 3 and self.big_fields * 10 <= self.fields
                and (self.texts or self.nested) and self.stopped_at is None)

    def message(self, start, end, nest, group=None):
        """(field nodes, end position) for the message in buf[start:end]; raises Invalid."""
        buf, ctx = self.buf, self.ctx
        nodes = []
        pos = start
        while pos < end:
            if self.stopped_at is not None or ctx.full() or not ctx.step():
                if self.stopped_at is None:
                    self.stopped_at = pos
                return nodes, end
            tag_at = pos
            key, pos = read_varint(buf, pos, end)
            number, wire = key >> 3, key & 7
            if wire == 4:
                if group is not None and number == group:
                    return nodes, pos
                raise Invalid("unexpected end-group at offset %d" % tag_at)
            if not 1 <= number <= MAX_FIELD or 19000 <= number <= 19999:
                raise Invalid("invalid field number %d at offset %d" % (number, tag_at))
            if wire == 0:
                value, pos = read_varint(buf, pos, end)
                node = self._varint(number, value)
            elif wire == 1 or wire == 5:
                size = 8 if wire == 1 else 4
                if pos + size > end:
                    raise Invalid("truncated fixed%d at offset %d" % (size * 8, tag_at))
                node = self._fixed(number, bytes(buf[pos:pos + size]))
                pos += size
            elif wire == 2:
                length, pos = read_varint(buf, pos, end)
                if length > end - pos:
                    raise Invalid("length %d at offset %d runs past the end" % (length, tag_at))
                node = self._payload(number, tag_at, pos, pos + length, nest)
                pos += length
            elif wire == 3:
                if nest >= ctx.protobuf_nest or not ctx.can_nest():
                    raise Invalid("groups nested too deeply at offset %d" % tag_at)
                ctx.nest += 1
                try:
                    children, pos = self.message(pos, end, nest + 1, group=number)
                finally:
                    ctx.nest -= 1
                node = ctx.node("protobuf", number, children=children, note="group")
                self.nested += 1
            else:
                raise Invalid("invalid wire type %d at offset %d" % (wire, tag_at))
            if node.kind != "bytes":
                node.offset, node.length = tag_at, pos - tag_at
            self.fields += 1
            if number > self.max_field:
                self.max_field = number
            if number > BIG_FIELD:
                self.big_fields += 1
            nodes.append(node)
        if group is not None:
            raise Invalid("group %d is never closed" % group)
        return nodes, pos

    # -- scalars ----------------------------------------------------------------
    def _varint(self, number, value):
        notes = ["varint"]
        signed = _signed(value, 64)
        zigzag = (value >> 1) ^ -(value & 1)
        if signed != value:
            notes.append("uint64 %d" % value)
        if zigzag != signed:
            notes.append("sint64 %d" % zigzag)
        if value in (0, 1):
            notes.append("bool %s" % ("true" if value else "false"))
        return self.ctx.node("int", number, signed, note=", ".join(notes))

    def _fixed(self, number, raw):
        bits = len(raw) * 8
        unsigned = int.from_bytes(raw, "little")
        signed = _signed(unsigned, bits)
        real = struct.unpack("<d" if bits == 64 else "<f", raw)[0]
        shown = repr(real) if bits == 64 else "%.9g" % real
        note = "fixed%d: %s %s, int%d %d, uint%d %d" % (
            bits, "double" if bits == 64 else "float", shown, bits, signed, bits, unsigned)
        if _plausible_float(real, 1e15 if bits == 64 else 1e9) and unsigned > 0xFFFFF:
            return self.ctx.node("float", number, real, note=note)
        return self.ctx.node("int", number, signed if signed < 0 else unsigned, note=note)

    # -- length-delimited --------------------------------------------------------
    def _payload(self, number, tag_at, start, end, nest):
        ctx, buf = self.ctx, self.buf
        if start == end:
            return ctx.node("string", number, "", note="empty (string, bytes or message)")
        snap = self._snapshot()
        message = None
        if nest < ctx.protobuf_nest and end - start >= 2 and ctx.can_nest():
            ctx.nest += 1
            try:
                message, _ = self.message(start, end, nest + 1)
            except Invalid:
                self._restore(snap)
                message = None
            finally:
                ctx.nest -= 1
            if message and len(message) < 3 and max(n.label for n in message) > BIG_FIELD:
                # One or two fields with a large number: typical of random bytes (IDs,
                # hashes) that happen to parse. Keep them as bytes.
                self._restore(snap)
                message = None
        as_text = _clean_utf8(buf, start, end)
        if message and (as_text is None or buf[start] < 0x20):
            self.nested += 1
            note = "nested message" + ("; also valid text" if as_text is not None else "")
            return ctx.node("protobuf", number, children=message, note=note)
        if message:
            self._restore(snap)         # read as text instead: drop the message's counts
        if as_text is not None:
            if len(as_text) >= 3:
                self.texts += 1
            note = "string" + ("; also parses as a nested message" if message else "")
            return ctx.string_node(as_text, self.depth, number, note=note)
        node = ctx.bytes_node(buf[start:end], self.depth + 1, number, offset=start,
                              length=end - start, note="field tag at offset %d" % tag_at,
                              skip=("protobuf",))
        packed = self._packed(start, end)
        if packed:
            node.children.append(ctx.node("array", "packed varints", children=packed,
                                          confidence=UNCERTAIN,
                                          note="alternative reading as packed varints"))
        return node

    def _packed(self, start, end):
        values, pos = [], start
        try:
            while pos < end:
                if len(values) >= self.ctx.protobuf_packed or not self.ctx.step():
                    return None
                value, pos = read_varint(self.buf, pos, end)
                values.append(value)
        except Invalid:
            return None
        if len(values) < 2 or len(values) == end - start and max(values) < 2:
            return None     # a run of 0/1 bytes says nothing
        return [self.ctx.node("int", i, _signed(v, 64)) for i, v in enumerate(values)]


def _clean_utf8(buf, start, end):
    try:
        s = bytes(buf[start:end]).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if text.is_clean_text(s) else None


def decode(data, ctx, depth):
    """Attempt to read the whole buffer as one protobuf message."""
    if len(data) < 2:
        return None
    parser = _Parser(data, ctx, depth)
    try:
        fields, _ = parser.message(0, len(data), 0)
    except Invalid as e:
        return failed("protobuf", str(e))
    except RecursionError:
        return failed("protobuf", "nested too deeply")
    if not fields:
        return failed("protobuf", "no fields")
    conf = CONFIDENT if parser.structured(len(data)) else UNCERTAIN
    note = "%d field%s" % (len(fields), "" if len(fields) == 1 else "s")
    if parser.stopped_at is not None:
        note += "; decode limit reached at offset %d, rest not validated" % parser.stopped_at
    node = ctx.node("protobuf", offset=0, length=len(data), children=fields, confidence=conf,
                    note=note)
    reason = "whole buffer parses as a message: %d fields in total, highest field number %d" \
        % (parser.fields, parser.max_field)
    return Attempt("protobuf", conf, node, reason)
