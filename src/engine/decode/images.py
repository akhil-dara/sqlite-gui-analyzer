"""Image dimensions from headers, for the formats where that is cheap (stdlib only).

dimensions(data, subtype) returns (width, height) or None; it never raises and reads at most
a bounded prefix of the buffer.
"""

import struct

_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_SCAN_LIMIT = 1 << 20       # bytes of JPEG/HEIF scanned for the size


def _png(data):
    if len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _gif(data):
    if len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    return None


def _bmp(data):
    header = int.from_bytes(data[14:18], "little")
    if header == 12 and len(data) >= 22:
        return struct.unpack("<HH", data[18:22])
    if len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return abs(w), abs(h)       # negative height = top-down rows
    return None


def _jpeg(data):
    i, end = 2, min(len(data), _SCAN_LIMIT)
    while i + 9 <= end:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:          # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD9, 0xDA) or size < 2:
            return None
        i += 2 + size
    return None


def _webp(data):
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        return (int.from_bytes(data[24:27], "little") + 1,
                int.from_bytes(data[27:30], "little") + 1)
    return None


def _heif(data):
    # The first 'ispe' (image spatial extent) property: version/flags, width, height.
    i = data.find(b"ispe", 0, _SCAN_LIMIT)
    if i < 0 or i + 16 > len(data):
        return None
    return struct.unpack(">II", data[i + 8:i + 16])


def _tiff(data):
    order = "<" if data[:2] == b"II" else ">"
    if len(data) < 8:
        return None
    ifd = struct.unpack(order + "I", data[4:8])[0]     # often at the end: random access
    if ifd + 2 > len(data):
        return None
    count = struct.unpack(order + "H", data[ifd:ifd + 2])[0]
    found = {}
    for n in range(min(count, 512)):
        at = ifd + 2 + 12 * n
        if at + 12 > len(data):
            break
        tag, typ = struct.unpack(order + "HH", data[at:at + 4])
        if tag in (256, 257):
            fmt = "H" if typ == 3 else "I"
            found[tag] = struct.unpack(order + fmt, data[at + 8:at + 8 + struct.calcsize(fmt)])[0]
    if 256 in found and 257 in found:
        return found[256], found[257]
    return None


_PARSERS = {"PNG": _png, "GIF": _gif, "BMP": _bmp, "JPEG": _jpeg, "WEBP": _webp,
            "HEIC": _heif, "HEIF": _heif, "AVIF": _heif, "TIFF": _tiff}


def dimensions(data, subtype):
    """(width, height) for an image of the given subtype (as named by detect.magic)."""
    parser = _PARSERS.get(subtype)
    if parser is None:
        return None
    try:
        size = parser(data)
    except (struct.error, IndexError, ValueError):
        return None
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    return int(size[0]), int(size[1])
