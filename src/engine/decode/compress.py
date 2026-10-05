"""Decompression: gzip, zlib, raw deflate, bzip2, xz, legacy lzma and zstd, plus LZ4 and
Apple's LZFSE / LZVN / LZ4 containers (decoded in pure Python by the lz module).

Every stream is decompressed with an output limit (decompressor max_length), so a small
"bomb" cannot expand beyond the Context's per-stream cap (64 MiB by default) or its total
budget; hitting the cap is reported in the node's note. bz2/lzma may be missing from a Python
build: missing support is reported, never raised. zstd is decoded by the pure-Python zstd
module on every Python. The xz / lzma decoders are held to the limit 'decode_lzma_memory'
(their dictionary size is declared by the data).
"""

import struct
import zlib

from . import lz, text, zstd
from .nodes import CONFIDENT, UNCERTAIN, Attempt, failed
from .timestamps import iso, to_datetime

try:
    import bz2
except ImportError:         # Python built without libbz2
    bz2 = None
try:
    import lzma
except ImportError:         # Python built without liblzma
    lzma = None

MAX_MEMBERS = 64            # members followed (the default of limits 'decode_members')
_NEXT_MEMBER = {"gzip": b"\x1f\x8b", "bz2": b"BZh", "xz": b"\xfd7zXZ\x00",
                "zstd": b"\x28\xb5\x2f\xfd"}
LABELS = {"gzip": "gzip", "zlib": "zlib", "deflate": "raw deflate", "bz2": "bzip2",
          "xz": "xz", "lzma": "lzma", "zstd": "zstd"}


def _factory(kind, lzma_memory=64 << 20):
    """A function creating a fresh decompressor object for kind, or None if unsupported.
    The xz / lzma decoders may take at most lzma_memory bytes (their dictionary size is
    declared by the data)."""
    if kind == "gzip":
        return lambda: zlib.decompressobj(31)
    if kind == "zlib":
        return lambda: zlib.decompressobj(15)
    if kind == "deflate":
        return lambda: zlib.decompressobj(-15)
    if kind == "bz2" and bz2 is not None:
        return bz2.BZ2Decompressor
    if kind == "xz" and lzma is not None:
        return lambda: lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=lzma_memory)
    if kind == "lzma" and lzma is not None:
        return lambda: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE, memlimit=lzma_memory)
    return None


def inflate_stream(kind, data, cap, lzma_memory=64 << 20, max_members=MAX_MEMBERS):
    """Decompress data (following concatenated members) producing at most cap bytes.

    Returns (output, status, members, trailing_bytes); status is "ok" (stream ended),
    "capped" (output limit reached) or "truncated" (input ended mid-stream). Raises the
    decompressor's error if the first member is invalid.
    """
    make = _factory(kind, lzma_memory)
    chunks, total, members, rest, status = [], 0, 0, data, "ok"
    while True:
        limit = cap - total
        if limit <= 0:
            status = "capped"
            break
        d = make()
        try:
            chunk = d.decompress(rest, limit)
        except Exception:       # noqa: BLE001 - zlib.error, OSError, LZMAError, ZstdError...
            if members == 0:
                raise
            break               # garbage after a complete member: reported as trailing bytes
        chunks.append(chunk)
        total += len(chunk)
        members += 1
        if not d.eof:
            status = "capped" if total >= cap else "truncated"
            rest = b""
            break
        rest = d.unused_data
        nxt = _NEXT_MEMBER.get(kind)
        if not rest or nxt is None or not rest.startswith(nxt) or members >= max_members:
            break
    return b"".join(chunks), status, members, rest


def _gzip_header_note(data):
    """Original name and modification time from a gzip header, when present."""
    notes = []
    try:
        flags = data[3]
        mtime = struct.unpack("<I", data[4:8])[0]
        pos = 10
        if flags & 4:
            pos += 2 + struct.unpack("<H", data[10:12])[0]
        if flags & 8:
            end = data.find(b"\x00", pos, pos + 1024)
            if end > pos:
                notes.append("name %r" % data[pos:end].decode("latin-1"))
        if mtime:
            dt = to_datetime(mtime, "unix_s")
            if dt is not None:
                notes.append("mtime " + iso(dt))
    except (IndexError, struct.error):
        pass
    return ", ".join(notes)


def _describe(kind, data, out, status, members, rest, cap):
    notes = ["%d → %d bytes" % (len(data), len(out))]
    if members > 1:
        notes.append("%d members" % members)
    if status == "capped":
        notes.append("output capped at %d bytes (decompression limit)" % cap)
    elif status == "truncated":
        notes.append("stream ends early (incomplete data)")
    if rest and rest.strip(b"\x00"):
        notes.append("%d trailing bytes after the stream" % len(rest))
    if kind == "gzip":
        header = _gzip_header_note(data)
        if header:
            notes.append(header)
    return ", ".join(notes)


def decode(data, ctx, depth, kind):
    """Attempt for a buffer whose signature says it is compressed with `kind`."""
    if kind in lz.KINDS:
        return lz.decode(data, ctx, depth, kind)
    if kind == "zstd":
        return _decode_zstd(data, ctx, depth)
    if kind == "bz2" and not (data[3:4].isdigit() and data[4:10] in (b"1AY&SY", b"\x17rE8P\x90")):
        return failed("bz2", "BZh signature without a bzip2 block header")
    if _factory(kind) is None:
        why = "this Python has no %s module" % ("bz2" if kind == "bz2" else "lzma")
        node = ctx.node(kind, offset=0, length=len(data), note=why)
        return Attempt(kind, CONFIDENT, node, "%s signature; %s" % (LABELS[kind], why))
    if kind == "lzma" and len(data) >= 5 and int.from_bytes(data[1:5], "little") > ctx.lzma_memory:
        ctx.limits_hit.add("lzma memory")
        why = ("not decoded: the stream declares a %s-byte dictionary, more than the limit "
               "decode_lzma_memory (%s bytes)" % (format(int.from_bytes(data[1:5], "little"), ","),
                                                  format(ctx.lzma_memory, ",")))
        node = ctx.node(kind, offset=0, length=len(data), note=why)
        return Attempt(kind, CONFIDENT, node, "%s signature; %s" % (LABELS[kind], why))
    cap = ctx.output_cap()
    if cap <= 0:
        ctx.limits_hit.add("output")
        return failed(kind, "decompression budget used up")
    try:
        out, status, members, rest = inflate_stream(kind, data, cap, ctx.lzma_memory, ctx.members)
    except Exception as e:      # noqa: BLE001 - any decompressor error means "not this format"
        if "memory usage limit" in str(e).lower():
            ctx.limits_hit.add("lzma memory")
            why = ("not decoded: the stream needs more memory than the limit decode_lzma_memory "
                   "(%s bytes)" % format(ctx.lzma_memory, ","))
            node = ctx.node(kind, offset=0, length=len(data), note=why)
            return Attempt(kind, CONFIDENT, node, "%s signature; %s" % (LABELS[kind], why))
        return failed(kind, "invalid %s stream: %s" % (LABELS[kind], str(e)[:80]))
    ctx.used_output(len(out))
    if status == "capped":
        ctx.limits_hit.add("output")
    if status == "truncated" and not out:
        return failed(kind, "%s stream ends before any output" % LABELS[kind])
    conf = UNCERTAIN if status == "truncated" else CONFIDENT
    note = _describe(kind, data, out, status, members, rest, cap)
    child = ctx.bytes_node(out, depth + 1, note="decompressed")
    node = ctx.node(kind, offset=0, length=len(data), children=[child], confidence=conf,
                    note=note)
    return Attempt(kind, conf, node, "%s stream: %s" % (LABELS[kind], note))


def _decode_zstd(data, ctx, depth):
    """zstd, decoded by the pure-Python decoder (the same on every Python)."""
    if len(data) > 4 and data[4] & 0x08:
        return failed("zstd", "zstd magic with a reserved frame-header bit set")
    cap = ctx.output_cap()
    if cap <= 0:
        ctx.limits_hit.add("output")
        return failed("zstd", "decompression budget used up")
    out, status, reason, end, info = zstd.decompress(data, cap, ctx.lz_frames,
                                                     ctx.checksum_bytes)
    if status in ("truncated", "corrupt") and not out:
        return failed("zstd", "invalid zstd stream: %s" % reason)
    ctx.used_output(len(out))
    notes = ["%d → %d bytes" % (len(data), len(out))]
    if info["frames"] > 1:
        notes.append("%d frames" % info["frames"])
    notes.extend(info["notes"])
    notes.extend(info["problems"])
    if status == "capped":
        ctx.limits_hit.add("output")
        notes.append("output capped at %d bytes (decompression limit)" % cap)
    elif status == "truncated":
        notes.append("stream ends early (%s)" % reason)
    elif status == "corrupt":
        notes.append("corrupt data: %s" % reason)
    elif end < len(data) and data[end:].strip(b"\x00"):
        notes.append("%d trailing bytes after the stream" % (len(data) - end))
    conf = CONFIDENT if status in ("ok", "capped") and not info["problems"] else UNCERTAIN
    note = ", ".join(notes)
    child = ctx.bytes_node(out, depth + 1, note="decompressed")
    node = ctx.node("zstd", offset=0, length=len(data), children=[child], confidence=conf,
                    note=note)
    return Attempt("zstd", conf, node, "zstd stream: %s" % note)


def decode_raw_deflate(data, ctx, depth):
    """Raw deflate has no signature: only offered (as uncertain) when the whole buffer
    inflates cleanly and the output is itself recognisable."""
    if len(data) < 4:
        return None
    cap = ctx.output_cap()
    if cap <= 0:
        return None
    try:
        out, status, _members, rest = inflate_stream("deflate", data, cap)
    except Exception:           # noqa: BLE001 - not deflate
        return failed("deflate", "not a raw deflate stream")
    if status != "ok" or rest or not out:
        return failed("deflate", "does not inflate as one complete raw deflate stream")
    ctx.used_output(len(out))
    child = ctx.bytes_node(out, depth + 1, note="inflated")
    if not (text.meaningful(child) or _clean_utf8(out)):
        return failed("deflate", "inflates, but the output is not recognisable")
    note = _describe("deflate", data, out, status, 1, rest, cap)
    node = ctx.node("deflate", offset=0, length=len(data), children=[child],
                    confidence=UNCERTAIN, note=note)
    return Attempt("deflate", UNCERTAIN, node, "inflates cleanly: " + note)


def _clean_utf8(out):
    try:
        return text.is_clean_text(out.decode("utf-8"))
    except UnicodeDecodeError:
        return False
