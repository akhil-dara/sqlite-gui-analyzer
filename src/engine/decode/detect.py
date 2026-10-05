"""Cheap format sniffing from leading bytes (no decoding happens here).

magic(data) names the one format whose signature the buffer starts with. Formats that can
be decoded are confirmed by decoding them; "file" types are identified by signature alone,
so their signatures are long or binary enough that ordinary text cannot start with them.
"""

import re

# (prefix, kind, subtype) for signatures that decide the format on their own.
_PREFIXES = (
    (b"bplist", "bplist", None),
    (b"\x04\x0bstreamtyped", "typedstream", None),
    (b"\x1f\x8b\x08", "gzip", None),
    (b"BZh", "bz2", None),
    (b"\xfd7zXZ\x00", "xz", None),
    (b"\x28\xb5\x2f\xfd", "zstd", None),
    (b"\x89PNG\r\n\x1a\n", "image", "PNG"),
    (b"\xff\xd8\xff", "image", "JPEG"),
    (b"GIF87a", "image", "GIF"),
    (b"GIF89a", "image", "GIF"),
    (b"II*\x00", "image", "TIFF"),
    (b"MM\x00*", "image", "TIFF"),
    (b"SQLite format 3\x00", "file", "SQLite database"),
    (b"%PDF-", "file", "PDF document"),
    (b"PK\x03\x04", "file", "ZIP archive"),
    (b"PK\x05\x06", "file", "ZIP archive (empty)"),
    (b"7z\xbc\xaf\x27\x1c", "file", "7-Zip archive"),
    (b"Rar!\x1a\x07", "file", "RAR archive"),
    (b"OggS\x00", "file", "Ogg media"),
    (b"fLaC\x00", "file", "FLAC audio"),
    (b"fLaC\x80", "file", "FLAC audio"),
    (b"ID3\x02", "file", "MP3 audio"),
    (b"ID3\x03", "file", "MP3 audio"),
    (b"ID3\x04", "file", "MP3 audio"),
    (b"#!AMR\n", "file", "AMR audio"),
    (b"#!AMR-WB\n", "file", "AMR-WB audio"),
    (b"caff\x00\x01", "file", "Core Audio file"),
    (b"\x1a\x45\xdf\xa3", "file", "Matroska/WebM media"),
    (b"\x7fELF", "file", "ELF executable"),
    (b"\xca\xfe\xba\xbe", "file", "Mach-O universal binary or Java class"),
    (b"\xcf\xfa\xed\xfe", "file", "Mach-O binary"),
    (b"\xce\xfa\xed\xfe", "file", "Mach-O binary"),
    (b"dex\n0", "file", "Android DEX"),
    (b"\x04\x22\x4d\x18", "lz4", None),
    (b"bv41", "lz4_apple", None),
    (b"bv4-", "lz4_apple", None),
    (b"bvx2", "lzfse", None),
    (b"bvx1", "lzfse", None),
    (b"bvx-", "lzfse", None),
    (b"bvxn", "lzvn", None),
)

_HEIF_BRANDS = {b"heic": "HEIC", b"heix": "HEIC", b"heim": "HEIC", b"heis": "HEIC",
                b"hevc": "HEIC", b"hevx": "HEIC", b"mif1": "HEIF", b"msf1": "HEIF",
                b"avif": "AVIF", b"avis": "AVIF"}
_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]+={0,2}\Z")
_BASE64URL_RE = re.compile(rb"[A-Za-z0-9_-]+={0,2}\Z")
_NOT_BASE64_CHAR = re.compile(rb"[^A-Za-z0-9+/=_\-\s]")
_LZMA_DICT_SIZES = frozenset([1 << n for n in range(12, 32)] +
                             [(1 << n) + (1 << (n - 1)) for n in range(12, 32)])


def zlib_header_ok(data):
    """RFC 1950 header: deflate method, window <= 32 KiB, check bits, no preset dictionary."""
    if len(data) < 3:
        return False
    cmf, flg = data[0], data[1]
    return (cmf & 0x0F) == 8 and (cmf >> 4) <= 7 and (cmf * 256 + flg) % 31 == 0 \
        and not flg & 0x20


def lzma_alone_header_ok(data):
    """Legacy .lzma header: properties byte, 2^n or 2^n + 2^(n-1) dictionary, sane size."""
    if len(data) < 14 or data[0] >= 225:
        return False
    if int.from_bytes(data[1:5], "little") not in _LZMA_DICT_SIZES:
        return False
    size = int.from_bytes(data[5:13], "little")
    return size == 0xFFFFFFFFFFFFFFFF or size < (1 << 40)


def xml_plist_start(data):
    """True if the buffer looks like an XML property list (optionally with a UTF-8 BOM)."""
    head = data[:1024]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    head = head.lstrip()
    if head.startswith(b"<plist"):
        return True
    return head.startswith(b"<?xml") and (b"<plist" in head or b"PropertyList" in head)


def magic(data):
    """(kind, subtype) for a signature the buffer starts with, else (None, None).

    subtype is a display name for "image" and "file" kinds and None otherwise.
    """
    if not data:
        return None, None
    for prefix, kind, subtype in _PREFIXES:
        if data.startswith(prefix):
            return kind, subtype
    if data.startswith(b"RIFF") and len(data) >= 12:
        form = data[8:12]
        if form == b"WEBP":
            return "image", "WEBP"
        if form == b"WAVE":
            return "file", "WAV audio"
        if form == b"AVI ":
            return "file", "AVI video"
    if len(data) >= 12 and data[4:8] == b"ftyp" and \
            8 <= int.from_bytes(data[:4], "big") <= 4096:
        brand = data[8:12]
        if brand in _HEIF_BRANDS:
            return "image", _HEIF_BRANDS[brand]
        if brand == b"qt  ":
            return "file", "QuickTime movie"
        if brand[:1].isalnum():
            return "file", "MP4/ISO media (%s)" % brand.decode("latin-1").strip()
    if data.startswith(b"BM") and len(data) >= 26 and \
            int.from_bytes(data[14:18], "little") in (12, 40, 52, 56, 64, 108, 124):
        return "image", "BMP"
    if xml_plist_start(data):
        return "xml_plist", None
    if zlib_header_ok(data):
        return "zlib", None
    if lzma_alone_header_ok(data):
        return "lzma", None
    return None, None


def looks_base64(data, min_length=16):
    """Strict base64 check on bytes: alphabet only, padded to a multiple of 4 once line breaks
    are removed (spaces or tabs inside disqualify: that is prose, not base64). Returns the
    compact text (bytes) and whether it is the URL-safe alphabet, or (None, False)."""
    if len(data) < min_length or _NOT_BASE64_CHAR.search(data[:256]):
        return None, False
    stripped = data.strip()
    if b" " in stripped or b"\t" in stripped:
        return None, False
    compact = stripped.replace(b"\r", b"").replace(b"\n", b"")
    if len(compact) < min_length or len(compact) % 4:
        return None, False
    if _BASE64_RE.match(compact):
        return compact, False
    if _BASE64URL_RE.match(compact):
        return compact, True
    return None, False


def first_non_space(data):
    """First byte that is not ASCII whitespace (after a UTF-8 BOM), or b''."""
    head = data[:64]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    head = head.lstrip()
    return head[:1]
