"""Varints, serial types and record decoding."""

import struct

ENCODINGS = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}

_FIXED_SIZES = (0, 1, 2, 3, 4, 6, 8, 8, 0, 0, 0, 0)
_DOUBLE = struct.Struct(">d")


class RecordError(ValueError):
    """The bytes do not form a valid SQLite record."""


class InvalidText(bytes):
    """A TEXT value whose bytes are not valid in the database text encoding.

    Kept as the raw bytes so nothing is lost; the UI renders it with a warning.
    """
    __slots__ = ()

    def __repr__(self):
        return "InvalidText(%s)" % bytes.__repr__(self)


def read_varint(buf, pos):
    """Return (value, new_pos) for the SQLite varint at buf[pos].

    1-8 bytes carry 7 bits each (high bit = continue); a 9th byte carries 8 bits.
    Raises RecordError if the buffer ends first.
    """
    value = 0
    end = len(buf)
    for i in range(8):
        if pos >= end:
            raise RecordError("truncated varint")
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if b < 0x80:
            return value, pos
    if pos >= end:
        raise RecordError("truncated varint")
    value = (value << 8) | buf[pos]
    return value, pos + 1


def to_signed64(value):
    """Varints are unsigned on disk; rowids are signed 64-bit."""
    return value - (1 << 64) if value >= (1 << 63) else value


def serial_size(serial_type):
    """Number of body bytes used by a value of this serial type."""
    if serial_type < 12:
        return _FIXED_SIZES[serial_type]
    return (serial_type - 12) >> 1 if serial_type % 2 == 0 else (serial_type - 13) >> 1


def decode_value(buf, pos, serial_type, encoding="utf-8", issues=None, where=""):
    """Decode one value. Types 10/11 (reserved) become None and log an issue."""
    if serial_type == 0:
        return None
    if 1 <= serial_type <= 6:
        n = _FIXED_SIZES[serial_type]
        return int.from_bytes(buf[pos:pos + n], "big", signed=True)
    if serial_type == 7:
        return _DOUBLE.unpack_from(buf, pos)[0]
    if serial_type == 8:
        return 0
    if serial_type == 9:
        return 1
    if serial_type in (10, 11):
        if issues is not None:
            issues.add("reserved_serial", "serial type %d is reserved" % serial_type, where)
        return None
    n = serial_size(serial_type)
    raw = bytes(buf[pos:pos + n])
    if serial_type % 2 == 0:
        return raw
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        if issues is not None:
            issues.add("invalid_text", "TEXT is not valid %s (%d bytes)" % (encoding, n), where)
        return InvalidText(raw)


def parse_header(payload):
    """Return (serial_types, body_offset) or raise RecordError."""
    header_len, pos = read_varint(payload, 0)
    if header_len < 1 or header_len > len(payload):
        raise RecordError("record header length %d out of range" % header_len)
    types = []
    while pos < header_len:
        st, pos = read_varint(payload, pos)
        types.append(st)
    if pos != header_len:
        raise RecordError("record header overruns its declared length")
    return types, header_len


def decode_record(payload, encoding="utf-8", issues=None, where=""):
    """Decode a complete record payload into a list of Python values (strict)."""
    values, problem = decode_record_lenient(payload, encoding, issues, where)
    if problem:
        raise RecordError(problem)
    return values


def decode_record_lenient(payload, encoding="utf-8", issues=None, where=""):
    """Decode as much of a record as the bytes allow.

    Returns (values, problem). problem is None for a well-formed record, otherwise a
    description; values then holds every column that could be decoded (possibly []).
    Used for evidence where damaged rows must be shown, never dropped. Every problem is also
    logged as a 'damaged_record' Issue when an IssueLog is given.
    """
    try:
        header_len, pos = read_varint(payload, 0)
    except RecordError as e:
        return _damaged([], str(e), issues, where)
    if header_len == 0:
        return _damaged([], "record header length is 0", issues, where)
    problem = None
    limit = header_len
    if header_len > len(payload):
        problem = "record header length %d exceeds payload %d" % (header_len, len(payload))
        limit = len(payload)
    types = []
    try:
        while pos < limit:
            st, pos = read_varint(payload, pos)
            types.append(st)
    except RecordError as e:
        problem = problem or str(e)
    if problem is None and pos != header_len:
        problem = "record header overruns its declared length"
    body = header_len if problem is None else len(payload)
    values = []
    for st in types:
        n = serial_size(st)
        if body + n > len(payload):
            have = len(payload) - body
            if st >= 12 and have > 0:
                # a TEXT or BLOB cut short (e.g. its overflow chain ends early): the bytes
                # that exist are kept as a partial value, never padded
                raw = bytes(payload[body:])
                if st & 1:
                    try:
                        raw = raw.decode(encoding)
                    except UnicodeDecodeError:
                        raw = InvalidText(raw)
                values.append(raw)
                problem = problem or ("record body truncated in column %d (%d of %d bytes)"
                                      % (len(values) - 1, have, n))
                break
            problem = problem or ("record body truncated at column %d" % len(values))
            break
        values.append(decode_value(payload, body, st, encoding, issues, where))
        body += n
    return _damaged(values, problem, issues, where) if problem else (values, None)


def _damaged(values, problem, issues, where):
    if issues is not None:
        issues.add("damaged_record", problem, where)
    return values, problem
