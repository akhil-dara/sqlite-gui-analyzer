"""Pure-Python decoders for LZ4 and Apple's LZFSE / LZVN / LZ4 containers.

Kinds (named by detect.magic from the leading bytes):
  lz4        LZ4 frame format (magic 04 22 4D 18): frame descriptor with its header checksum,
             optional content size and dictionary id, compressed or stored blocks, optional
             block and content checksums (xxHash32, all verified). Linked and independent
             blocks are supported; skippable frames are skipped; concatenated frames followed.
  lz4_apple  Apple's LZ4 block stream: "bv41" (LZ4 block) and "bv4-" (stored) blocks ended
             by "bv4$". Matches may reach back into earlier blocks.
  lzfse, lzvn
             Apple's LZFSE container: "bvx2" / "bvx1" (LZFSE, packed or plain header),
             "bvxn" (LZVN) and "bvx-" (stored) blocks ended by "bvx$". The kind is lzvn when the
             stream starts with an LZVN block, else lzfse; both decode every block type.

Everything is bounded: output stops at the Context's per-stream cap (the node note says
"output capped"), no buffer is sized from a declared length, and every loop consumes input
or is bounded by per-block limits. Malformed input never raises: it gives a failed attempt,
or an uncertain result keeping the output produced before the problem.
"""

import struct

from .nodes import CONFIDENT, UNCERTAIN, Attempt, failed

KINDS = frozenset(("lz4", "lz4_apple", "lzfse", "lzvn"))
LABELS = {"lz4": "LZ4 frame", "lz4_apple": "Apple LZ4", "lzfse": "LZFSE", "lzvn": "LZVN"}

MAX_CHECKSUM_BYTES = 16 << 20   # xxHash32 over more than this is skipped (pure Python is slow)
MAX_FRAMES = 4096               # LZ4 frames (and skippable frames) followed in one buffer

_M32 = 0xFFFFFFFF
_P1, _P2, _P3, _P4, _P5 = 2654435761, 2246822519, 3266489917, 668265263, 374761393
_U32 = struct.Struct("<I")
_STRIPES = struct.Struct("<4I")


class _Bad(Exception):
    """Malformed input: decoding stops, the output produced so far is kept."""


class _Short(_Bad):
    """The input ends in the middle of the stream."""


class _Full(Exception):
    """The output limit was reached."""


# -- xxHash32 -------------------------------------------------------------------------------
def xxh32(data, seed=0):
    """32-bit xxHash of data (bytes-like) with the given seed."""
    data = bytes(data)
    n = len(data)
    i = 0
    if n >= 16:
        v1 = (seed + _P1 + _P2) & _M32
        v2 = (seed + _P2) & _M32
        v3 = seed & _M32
        v4 = (seed - _P1) & _M32
        limit = n - (n & 15)
        for a, b, c, d in _STRIPES.iter_unpack(data[:limit]):
            v1 = (v1 + a * _P2) & _M32
            v1 = (((v1 << 13) | (v1 >> 19)) * _P1) & _M32
            v2 = (v2 + b * _P2) & _M32
            v2 = (((v2 << 13) | (v2 >> 19)) * _P1) & _M32
            v3 = (v3 + c * _P2) & _M32
            v3 = (((v3 << 13) | (v3 >> 19)) * _P1) & _M32
            v4 = (v4 + d * _P2) & _M32
            v4 = (((v4 << 13) | (v4 >> 19)) * _P1) & _M32
        h = (((v1 << 1) | (v1 >> 31)) + ((v2 << 7) | (v2 >> 25)) +
             ((v3 << 12) | (v3 >> 20)) + ((v4 << 18) | (v4 >> 14))) & _M32
        i = limit
    else:
        h = (seed + _P5) & _M32
    h = (h + n) & _M32
    while i + 4 <= n:
        h = (h + _U32.unpack_from(data, i)[0] * _P3) & _M32
        h = (((h << 17) | (h >> 15)) * _P4) & _M32
        i += 4
    while i < n:
        h = (h + data[i] * _P5) & _M32
        h = (((h << 11) | (h >> 21)) * _P1) & _M32
        i += 1
    h ^= h >> 15
    h = (h * _P2) & _M32
    h ^= h >> 13
    h = (h * _P3) & _M32
    h ^= h >> 16
    return h


# -- output helpers -------------------------------------------------------------------------
def _put(out, chunk, limit):
    """Append chunk to out, stopping (with _Full) at limit bytes of output."""
    room = limit - len(out)
    if len(chunk) > room:
        if room > 0:
            out += chunk[:room]
        raise _Full()
    out += chunk


def _copy_match(out, dist, n, limit, low):
    """Append n bytes copied from dist bytes back (the copy may overlap its own output);
    the source must not start before out[low]."""
    start = len(out) - dist
    if dist <= 0 or start < low:
        raise _Bad("match distance %d reaches outside the output" % dist)
    room = limit - len(out)
    full = n > room
    if full:
        n = max(room, 0)
    if dist >= n:
        out += out[start:start + n]
    else:
        out += (out[start:] * (n // dist + 1))[:n]
    if full:
        raise _Full()


def _u32(data, pos, what):
    if pos + 4 > len(data):
        raise _Short("%s cut off" % what)
    return _U32.unpack_from(data, pos)[0]


# -- LZ4 ------------------------------------------------------------------------------------
def lz4_block(src, pos, end, out, limit, low=0):
    """Decode the raw LZ4 block src[pos:end], appending to out (a bytearray).

    Matches may reach back to out[low]. A block normally ends with a literals-only sequence;
    one that ends right after a match is accepted too. Raises _Short/_Bad/_Full.
    """
    while pos < end:
        token = src[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if pos >= end:
                    raise _Short("LZ4 literal length runs past the block")
                b = src[pos]
                pos += 1
                lit += b
                if b != 255:
                    break
        if lit:
            if pos + lit > end:
                _put(out, src[pos:end], limit)
                raise _Short("LZ4 literals run past the block")
            _put(out, src[pos:pos + lit], limit)
            pos += lit
        if pos >= end:
            return
        if pos + 2 > end:
            raise _Short("LZ4 match offset cut off")
        dist = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if dist == 0:
            raise _Bad("LZ4 match with offset 0")
        mlen = token & 15
        if mlen == 15:
            while True:
                if pos >= end:
                    raise _Short("LZ4 match length runs past the block")
                b = src[pos]
                pos += 1
                mlen += b
                if b != 255:
                    break
        _copy_match(out, dist, mlen + 4, limit, low)


_LZ4_MAGIC = 0x184D2204
_LZ4_BLOCK_MAX = {4: 64 << 10, 5: 256 << 10, 6: 1 << 20, 7: 4 << 20}


def _lz4_frame(data, pos, out, cap, info):
    """One LZ4 frame whose descriptor starts at pos (just after the magic). Returns the
    position after the frame."""
    n = len(data)
    desc = pos
    if pos + 3 > n:
        raise _Short("LZ4 frame descriptor cut off")
    flg, bd = data[pos], data[pos + 1]
    if flg >> 6 != 1:
        raise _Bad("unsupported LZ4 frame version %d" % (flg >> 6))
    if flg & 0x02 or bd & 0x8F:
        raise _Bad("reserved bits set in the LZ4 frame descriptor")
    block_max = _LZ4_BLOCK_MAX.get((bd >> 4) & 7)
    if block_max is None:
        raise _Bad("invalid LZ4 block maximum size code %d" % ((bd >> 4) & 7))
    independent, block_sums, content_sum = flg & 0x20, flg & 0x10, flg & 0x04
    pos += 2
    content_size = None
    if flg & 0x08:
        if pos + 8 > n:
            raise _Short("LZ4 content size cut off")
        content_size = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
    if flg & 0x01:
        info["notes"].append("dictionary id %d" % _u32(data, pos, "LZ4 dictionary id"))
        pos += 4
    if pos >= n:
        raise _Short("LZ4 header checksum cut off")
    if (xxh32(data[desc:pos]) >> 8) & 0xFF != data[pos]:
        info["problems"].append("frame header checksum mismatch")
    pos += 1
    start = len(out)
    while True:
        word = _u32(data, pos, "LZ4 block size")
        pos += 4
        if word == 0:
            break
        size = word & 0x7FFFFFFF
        if size > block_max:
            raise _Bad("LZ4 block of %d bytes exceeds the frame's %d-byte maximum"
                       % (size, block_max))
        end = pos + size
        block_start = len(out)
        low = block_start if independent else start
        info["blocks"] += 1
        if word & 0x80000000:
            _put(out, data[pos:min(end, n)], cap)
        else:
            lz4_block(data, pos, min(end, n), out, cap, low)
        if end > n:
            raise _Short("LZ4 block cut off")
        if block_sums:
            stored = _u32(data, end, "LZ4 block checksum")
            if size <= info["checksum_bytes"] and xxh32(data[pos:end]) != stored:
                info["problems"].append("block %d checksum mismatch" % info["blocks"])
            end += 4
        pos = end
    produced = len(out) - start
    if content_sum:
        stored = _u32(data, pos, "LZ4 content checksum")
        pos += 4
        if produced > info["checksum_bytes"]:
            info["notes"].append("content checksum not verified (output over limit "
                                 "decode_checksum_bytes)")
        elif xxh32(out[start:]) != stored:
            info["problems"].append("content checksum mismatch")
        else:
            info["verified"] = True
    if content_size is not None and content_size != produced:
        info["problems"].append("frame declares %d bytes of content, decoded %d"
                                % (content_size, produced))
    return pos


def _lz4_frames(data, out, cap, info):
    pos, n, frames = 0, len(data), 0
    for _ in range(info["max_frames"]):
        if pos + 4 > n:
            break
        magic = _U32.unpack_from(data, pos)[0]
        if 0x184D2A50 <= magic <= 0x184D2A5F:
            size = _u32(data, pos + 4, "skippable frame size")
            pos += 8 + size
            if pos > n:
                raise _Short("skippable frame cut off")
            info["notes"].append("skippable frame of %d bytes" % size)
            continue
        if magic != _LZ4_MAGIC:
            break
        pos = _lz4_frame(data, pos + 4, out, cap, info)
        frames += 1
        info["frames"] = frames
    return pos


def _lz4_apple(data, out, cap, info):
    pos, n = 0, len(data)
    while True:
        if pos + 4 > n:
            raise _Short("stream ends without the bv4$ end marker")
        tag = data[pos:pos + 4]
        if tag == b"bv4$":
            return pos + 4
        info["blocks"] += 1
        if tag == b"bv4-":
            size = _u32(data, pos + 4, "stored block size")
            pos += 8
            _put(out, data[pos:pos + size], cap)
            if pos + size > n:
                raise _Short("stored block cut off")
            pos += size
        elif tag == b"bv41":
            raw = _u32(data, pos + 4, "LZ4 block header")
            size = _u32(data, pos + 8, "LZ4 block header")
            pos += 12
            start = len(out)
            lz4_block(data, pos, min(pos + size, n), out, cap, 0)
            if pos + size > n:
                raise _Short("LZ4 block cut off")
            if len(out) - start != raw:
                info["problems"].append("block %d decodes to %d bytes, header says %d"
                                        % (info["blocks"], len(out) - start, raw))
            pos += size
        else:
            raise _Bad("unknown block marker %r" % bytes(tag))


# -- LZVN -----------------------------------------------------------------------------------
_SML_D, _MED_D, _LRG_D, _PRE_D, _SML_M, _LRG_M, _SML_L, _LRG_L, _NOP, _EOS, _UDEF = range(11)


def _lzvn_opcodes():
    table = []
    for op in range(256):
        low3, high = op & 7, op >> 4
        if op == 0xE0:
            kind = _LRG_L
        elif high == 0xE:
            kind = _SML_L
        elif op == 0xF0:
            kind = _LRG_M
        elif high == 0xF:
            kind = _SML_M
        elif 0xA0 <= op <= 0xBF:
            kind = _MED_D
        elif high in (0x7, 0xD):
            kind = _UDEF
        elif low3 == 7:
            kind = _LRG_D
        elif low3 == 6:
            if op < 0x40:
                kind = {0x06: _EOS, 0x0E: _NOP, 0x16: _NOP}.get(op, _UDEF)
            else:
                kind = _PRE_D
        else:
            kind = _SML_D
        table.append(kind)
    return tuple(table)


_LZVN_OPS = _lzvn_opcodes()


def lzvn_block(src, pos, end, out, limit, low=0):
    """Decode LZVN opcodes from src[pos:end], appending to out. Returns (position, saw
    end-of-stream). Raises _Short/_Bad/_Full (_Full also when out reaches limit)."""
    ops = _LZVN_OPS
    d = 0
    while pos < end:
        op = src[pos]
        kind = ops[op]
        if kind == _SML_D:
            if pos + 2 > end:
                raise _Short("LZVN opcode cut off")
            lit, mlen = op >> 6, ((op >> 3) & 7) + 3
            dist = ((op & 7) << 8) | src[pos + 1]
            pos += 2
        elif kind == _MED_D:
            if pos + 3 > end:
                raise _Short("LZVN opcode cut off")
            word = src[pos + 1] | (src[pos + 2] << 8)
            lit, mlen = (op >> 3) & 3, (((op & 7) << 2) | (word & 3)) + 3
            dist = word >> 2
            pos += 3
        elif kind == _LRG_D:
            if pos + 3 > end:
                raise _Short("LZVN opcode cut off")
            lit, mlen = op >> 6, ((op >> 3) & 7) + 3
            dist = src[pos + 1] | (src[pos + 2] << 8)
            pos += 3
        elif kind == _PRE_D:
            lit, mlen, dist = op >> 6, ((op >> 3) & 7) + 3, d
            pos += 1
        elif kind == _SML_M:
            lit, mlen, dist = 0, op & 15, d
            pos += 1
        elif kind == _LRG_M:
            if pos + 2 > end:
                raise _Short("LZVN opcode cut off")
            lit, mlen, dist = 0, src[pos + 1] + 16, d
            pos += 2
        elif kind == _SML_L:
            lit, mlen, dist = op & 15, 0, d
            pos += 1
        elif kind == _LRG_L:
            if pos + 2 > end:
                raise _Short("LZVN opcode cut off")
            lit, mlen, dist = src[pos + 1] + 16, 0, d
            pos += 2
        elif kind == _NOP:
            pos += 1
            continue
        elif kind == _EOS:
            return min(pos + 8, end), True
        else:
            raise _Bad("undefined LZVN opcode 0x%02x" % op)
        if lit:
            if pos + lit > end:
                _put(out, src[pos:end], limit)
                raise _Short("LZVN literals cut off")
            _put(out, src[pos:pos + lit], limit)
            pos += lit
        if mlen:
            _copy_match(out, dist, mlen, limit, low)
            d = dist
    return pos, False


# -- LZFSE ----------------------------------------------------------------------------------
def _bases(extra):
    base = [0]
    for e in extra[:-1]:
        base.append(base[-1] + (1 << e))
    return tuple(base)


L_EXTRA_BITS = (0,) * 16 + (2, 3, 5, 8)
M_EXTRA_BITS = (0,) * 16 + (3, 5, 8, 11)
D_EXTRA_BITS = tuple(i >> 2 for i in range(64))
L_BASE_VALUE = _bases(L_EXTRA_BITS)
M_BASE_VALUE = _bases(M_EXTRA_BITS)
D_BASE_VALUE = _bases(D_EXTRA_BITS)
L_STATES, M_STATES, D_STATES, LITERAL_STATES = 64, 64, 256, 1024
L_SYMBOLS, M_SYMBOLS, D_SYMBOLS, LITERAL_SYMBOLS = 20, 20, 64, 256
N_FREQ = L_SYMBOLS + M_SYMBOLS + D_SYMBOLS + LITERAL_SYMBOLS
MATCHES_PER_BLOCK = 10000
LITERALS_PER_BLOCK = 4 * MATCHES_PER_BLOCK
V1_HEADER = struct.Struct("<6Ii4Hi3H%dH" % N_FREQ)    # fields after the magic: 766 bytes,
V1_HEADER_SIZE = 772                                   # 770 with it, padded to 4-byte units
V2_FIXED = struct.Struct("<3Q")

# Frequency codes of the packed (v2) header, indexed by the low 5 bits: (bits, value);
# value None means a longer code (8 bits: 8 + next 4 bits; 14 bits: 24 + next 10 bits).
_FREQ_CODE = []
for _b in range(32):
    if _b & 3 == 0:
        _FREQ_CODE.append((2, 0))
    elif _b & 3 == 2:
        _FREQ_CODE.append((2, 1))
    elif _b & 7 == 1:
        _FREQ_CODE.append((3, 2))
    elif _b & 7 == 5:
        _FREQ_CODE.append((3, 3))
    elif _b & 7 == 3:
        _FREQ_CODE.append((5, 4 + (_b >> 3)))
    elif _b & 15 == 7:
        _FREQ_CODE.append((8, None))
    else:
        _FREQ_CODE.append((14, None))
_FREQ_CODE = tuple(_FREQ_CODE)
del _b


class _Bits(object):
    """Backward bit reader: the payload is one little-endian number whose top `-extra`
    bits are unused; bits are pulled from the most significant end downwards."""

    __slots__ = ("data", "low", "pos", "acc", "n")

    def __init__(self, data, low, end, extra):
        if not -7 <= extra <= 0:
            raise _Bad("invalid bit-stream padding %d" % extra)
        self.data, self.low, self.pos, self.acc, self.n = data, low, end, 0, 0
        if end > low:
            self.pos = end - 1
            self.acc = data[end - 1]
            self.n = 8 + extra
            if self.acc >> self.n:
                raise _Bad("bit-stream padding bits are not zero")
        elif extra:
            raise _Bad("empty bit stream with padding")

    def pull(self, k):
        if k > self.n:
            m = min((64 - self.n) >> 3, self.pos - self.low)
            if m > 0:
                self.pos -= m
                self.acc = (self.acc << (8 * m)) | \
                    int.from_bytes(self.data[self.pos:self.pos + m], "little")
                self.n += 8 * m
            if k > self.n:
                raise _Bad("bit stream ends early")
        self.n -= k
        v = self.acc >> self.n
        self.acc &= (1 << self.n) - 1
        return v


def _fse_entries(nstates, freqs, what):
    """(symbol, k, delta) for each state of a decoder table, None for unused states."""
    table = [None] * nstates
    top = nstates.bit_length()
    s = 0
    for sym, f in enumerate(freqs):
        if not f:
            continue
        if s + f > nstates:
            raise _Bad("%s frequencies add up to more than %d" % (what, nstates))
        k = top - f.bit_length()
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table[s] = (sym, k, ((f + j) << k) - nstates)
            else:
                table[s] = (sym, k - 1, (j - j0) << (k - 1))
            s += 1
    return table


def _value_table(nstates, freqs, extra, base, what):
    """Decoder table for L/M/D: (total bits, extra bits, delta, base value) per state."""
    return [None if e is None else (e[1] + extra[e[0]], extra[e[0]], e[2], base[e[0]])
            for e in _fse_entries(nstates, freqs, what)]


def _v2_freqs(data, start, end):
    """The 360 frequencies of a packed header, from data[start:end]."""
    if end == start:
        return [0] * N_FREQ
    freqs = []
    acc, nbits, pos = 0, 0, start
    for _ in range(N_FREQ):
        while pos < end and nbits + 8 <= 32:
            acc |= data[pos] << nbits
            nbits += 8
            pos += 1
        size, value = _FREQ_CODE[acc & 31]
        if size == 8:
            value = 8 + ((acc >> 4) & 0xF)
        elif size == 14:
            value = 24 + ((acc >> 4) & 0x3FF)
        if size > nbits:
            raise _Bad("frequency table runs past the block header")
        freqs.append(value)
        acc >>= size
        nbits -= size
    if nbits >= 8 or pos != end:
        raise _Bad("frequency table does not fill the block header exactly")
    return freqs


def _field(v, offset, bits):
    return (v >> offset) & ((1 << bits) - 1)


def _lzfse_header(data, pos, v2):
    """Header fields of a bvx1/bvx2 block at pos: (dict, header size)."""
    n = len(data)
    if v2:
        if pos + 32 > n:
            raise _Short("LZFSE block header cut off")
        raw = _U32.unpack_from(data, pos + 4)[0]
        f0, f1, f2 = V2_FIXED.unpack_from(data, pos + 8)
        size = _field(f2, 0, 32)
        if size < 32:
            raise _Bad("LZFSE block header size %d too small" % size)
        if pos + size > n:
            raise _Short("LZFSE block header cut off")
        h = {"raw": raw,
             "n_literals": _field(f0, 0, 20), "lit_bytes": _field(f0, 20, 20),
             "n_matches": _field(f0, 40, 20), "lit_bits": _field(f0, 60, 3) - 7,
             "lit_states": [_field(f1, 10 * i, 10) for i in range(4)],
             "lmd_bytes": _field(f1, 40, 20), "lmd_bits": _field(f1, 60, 3) - 7,
             "l_state": _field(f2, 32, 10), "m_state": _field(f2, 42, 10),
             "d_state": _field(f2, 52, 10),
             "freqs": _v2_freqs(data, pos + 32, pos + size)}
        return h, size
    if pos + V1_HEADER_SIZE > n:
        raise _Short("LZFSE block header cut off")
    v = V1_HEADER.unpack_from(data, pos + 4)
    h = {"raw": v[0], "n_literals": v[2], "n_matches": v[3], "lit_bytes": v[4],
         "lmd_bytes": v[5], "lit_bits": v[6], "lit_states": list(v[7:11]),
         "lmd_bits": v[11], "l_state": v[12], "m_state": v[13], "d_state": v[14],
         "freqs": list(v[15:])}
    return h, V1_HEADER_SIZE


def _lzfse_block(data, pos, v2, out, cap, info):
    """One bvx1/bvx2 block at pos; returns the position after it."""
    h, size = _lzfse_header(data, pos, v2)
    if h["n_literals"] > LITERALS_PER_BLOCK or h["n_matches"] > MATCHES_PER_BLOCK:
        raise _Bad("LZFSE block declares more literals or matches than a block holds")
    if any(s >= LITERAL_STATES for s in h["lit_states"]) or h["l_state"] >= L_STATES or \
            h["m_state"] >= M_STATES or h["d_state"] >= D_STATES:
        raise _Bad("LZFSE initial decoder state out of range")
    lit_start = pos + size
    lit_end = lit_start + h["lit_bytes"]
    lmd_end = lit_end + h["lmd_bytes"]
    if lmd_end > len(data):
        raise _Short("LZFSE block payload cut off")
    freqs = h["freqs"]
    a, b, c = L_SYMBOLS, L_SYMBOLS + M_SYMBOLS, L_SYMBOLS + M_SYMBOLS + D_SYMBOLS
    l_tab = _value_table(L_STATES, freqs[:a], L_EXTRA_BITS, L_BASE_VALUE, "L")
    m_tab = _value_table(M_STATES, freqs[a:b], M_EXTRA_BITS, M_BASE_VALUE, "M")
    d_tab = _value_table(D_STATES, freqs[b:c], D_EXTRA_BITS, D_BASE_VALUE, "D")
    lit_tab = _fse_entries(LITERAL_STATES, freqs[c:], "literal")

    # Literals: four interleaved states, read backwards from the end of their payload.
    n_lit = h["n_literals"]
    lits = bytearray()
    bits = _Bits(data, lit_start, lit_end, h["lit_bits"])
    states = h["lit_states"]
    for i in range((n_lit + 3) & ~3):
        q = i & 3
        e = lit_tab[states[q]]
        if e is None:
            raise _Bad("LZFSE literal state outside the frequency table")
        lits.append(e[0])
        states[q] = e[2] + bits.pull(e[1])

    # L, M, D triples.
    bits = _Bits(data, lit_end, lmd_end, h["lmd_bits"])
    ls, ms, ds = h["l_state"], h["m_state"], h["d_state"]
    dist, li, start = -1, 0, len(out)
    pull = bits.pull
    for _ in range(h["n_matches"]):
        e, f, g = l_tab[ls], m_tab[ms], d_tab[ds]
        if e is None or f is None or g is None:
            raise _Bad("LZFSE match state outside the frequency table")
        x = pull(e[0])
        ls = e[2] + (x >> e[1])
        lit = e[3] + (x & ((1 << e[1]) - 1))
        x = pull(f[0])
        ms = f[2] + (x >> f[1])
        mlen = f[3] + (x & ((1 << f[1]) - 1))
        x = pull(g[0])
        ds = g[2] + (x >> g[1])
        new = g[3] + (x & ((1 << g[1]) - 1))
        if new:
            dist = new
        if ls >= L_STATES or ms >= M_STATES or ds >= D_STATES:
            raise _Bad("LZFSE decoder state out of range")
        if lit:
            if li + lit > n_lit:
                raise _Bad("LZFSE match uses more literals than the block has")
            _put(out, lits[li:li + lit], cap)
            li += lit
        if mlen:
            _copy_match(out, dist, mlen, cap, 0)
    if len(out) - start != h["raw"]:
        info["problems"].append("LZFSE block decodes to %d bytes, header says %d"
                                % (len(out) - start, h["raw"]))
    return lmd_end


def _lzfse(data, out, cap, info):
    pos, n = 0, len(data)
    while True:
        if pos + 4 > n:
            raise _Short("stream ends without the bvx$ end marker")
        tag = data[pos:pos + 4]
        if tag == b"bvx$":
            return pos + 4
        info["blocks"] += 1
        info["types"].add(tag[3:4].decode("latin-1"))
        if tag == b"bvx-":
            size = _u32(data, pos + 4, "stored block size")
            pos += 8
            _put(out, data[pos:pos + size], cap)
            if pos + size > n:
                raise _Short("stored block cut off")
            pos += size
        elif tag == b"bvxn":
            raw = _u32(data, pos + 4, "LZVN block header")
            size = _u32(data, pos + 8, "LZVN block header")
            pos += 12
            start = len(out)
            limit = min(cap, start + raw)
            ended = False
            try:
                ended = lzvn_block(data, pos, min(pos + size, n), out, limit)[1]
            except _Full:
                if len(out) >= cap:
                    raise
                ended = True        # the block's declared size is complete
            if pos + size > n and not ended:
                raise _Short("LZVN block cut off")
            if not ended:
                info["problems"].append("LZVN block %d has no end-of-stream opcode"
                                        % info["blocks"])
            if len(out) - start != raw:
                info["problems"].append("LZVN block decodes to %d bytes, header says %d"
                                        % (len(out) - start, raw))
            pos += size
        elif tag in (b"bvx1", b"bvx2"):
            pos = _lzfse_block(data, pos, tag == b"bvx2", out, cap, info)
        else:
            raise _Bad("unknown block marker %r" % bytes(tag))


_RUNNERS = {"lz4": _lz4_frames, "lz4_apple": _lz4_apple, "lzfse": _lzfse, "lzvn": _lzfse}
_BLOCK_NAMES = {"2": "LZFSE", "1": "LZFSE v1", "n": "LZVN", "-": "stored"}


# -- attempt --------------------------------------------------------------------------------
def decompress(data, kind, cap, max_frames=MAX_FRAMES, checksum_bytes=MAX_CHECKSUM_BYTES):
    """(output bytes, status, reason, end position, info) for a buffer of this kind.

    status is "ok", "capped", "truncated" or "corrupt"; reason explains the last two.
    Never raises.
    """
    data = bytes(data)
    out = bytearray()
    info = {"problems": [], "notes": [], "blocks": 0, "frames": 0, "types": set(),
            "max_frames": max_frames, "checksum_bytes": checksum_bytes}
    status, reason, end = "ok", "", len(data)
    try:
        end = _RUNNERS[kind](data, out, cap, info)
    except _Full:
        status = "capped"
    except _Short as e:
        status, reason = "truncated", str(e)
    except _Bad as e:
        status, reason = "corrupt", str(e)
    except Exception as e:      # noqa: BLE001 - any other slip is malformed input
        status, reason = "corrupt", "malformed data (%s)" % type(e).__name__
    return bytes(out), status, reason, end, info


def decode(data, ctx, depth, kind):
    """Attempt for a buffer whose signature says it is `kind` (one of KINDS)."""
    label = LABELS[kind]
    cap = ctx.output_cap()
    if cap <= 0:
        ctx.limits_hit.add("output")
        return failed(kind, "decompression budget used up")
    out, status, reason, end, info = decompress(data, kind, cap, ctx.lz_frames,
                                                ctx.checksum_bytes)
    if status in ("truncated", "corrupt") and not out:
        return failed(kind, "invalid %s stream: %s" % (label, reason))
    ctx.used_output(len(out))
    notes = ["%d → %d bytes" % (len(data), len(out))]
    if info["frames"] > 1:
        notes.append("%d frames" % info["frames"])
    if info["blocks"] > 1:
        notes.append("%d blocks" % info["blocks"])
    if info["types"] and info["types"] != set("2"):
        notes.append("block types: " + ", ".join(
            _BLOCK_NAMES.get(t, t) for t in sorted(info["types"])))
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
    node = ctx.node(kind, offset=0, length=len(data), children=[child], confidence=conf,
                    note=note)
    return Attempt(kind, conf, node, "%s stream: %s" % (label, note))
