"""Pure-Python Zstandard decoder (RFC 8878 frames).

Supported: Zstandard frames (magic 28 B5 2F FD) with or without a content size, single-segment
or windowed; Raw, RLE and Compressed blocks; literals stored raw, as a run, or Huffman-coded
in one or four streams (the Huffman table described by FSE-compressed or direct 4-bit
weights, or reused from an earlier block); sequences with predefined, RLE, FSE-compressed
and repeated code tables; repeat offsets; the content checksum (low 32 bits of xxHash64,
verified). Skippable frames are skipped and concatenated frames followed. A frame that names
a dictionary is reported as needing it, never decoded with a guess.

Everything is bounded: output stops at `cap` bytes (status "capped"), no buffer is sized from
a declared length (content size, window size, block size and literal sizes only bound what is
accepted), the window is the output itself, every loop consumes input or is bounded by the
format's per-block limits, and table logs are checked against the format maxima. Malformed
input never raises: decoding stops and the output produced before the problem is kept.
"""

import struct

MAX_CHECKSUM_BYTES = 16 << 20   # xxHash64 over more output than this is skipped (slow)
MAX_FRAMES = 4096               # frames (skippable ones included) followed in one buffer
BLOCK_SIZE_MAX = 128 << 10      # absolute Block_Maximum_Size
HUF_MAX_BITS = 11               # longest Huffman code
LARGE_WINDOW = 1 << 31          # windows above this are noted (other decoders often refuse them)

MAGIC = 0xFD2FB528
_U32 = struct.Struct("<I")
_3H = struct.Struct("<3H")
_STRIPES = struct.Struct("<4Q")
_Q = struct.Struct("<Q")


class _Bad(Exception):
    """Malformed input: decoding stops, the output produced so far is kept."""


class _Short(_Bad):
    """The input ends in the middle of the stream."""


class _Full(Exception):
    """The output limit was reached."""


# -- xxHash64 -------------------------------------------------------------------------------
_M64 = (1 << 64) - 1
_P1, _P2, _P3 = 11400714785074694791, 14029467366897019727, 1609587929392839161
_P4, _P5 = 9650029242287828579, 2870177450012600261


def _round(acc, lane):
    acc = (acc + lane * _P2) & _M64
    return (((acc << 31) | (acc >> 33)) * _P1) & _M64


def _merge(h, v):
    h ^= _round(0, v)
    return (h * _P1 + _P4) & _M64


def xxh64(data, seed=0):
    """64-bit xxHash of data (bytes-like) with the given seed."""
    data = bytes(data)
    n = len(data)
    i = 0
    seed &= _M64
    if n >= 32:
        v1 = (seed + _P1 + _P2) & _M64
        v2 = (seed + _P2) & _M64
        v3 = seed
        v4 = (seed - _P1) & _M64
        limit = n - (n & 31)
        for a, b, c, d in _STRIPES.iter_unpack(data[:limit]):
            v1 = (v1 + a * _P2) & _M64
            v1 = (((v1 << 31) | (v1 >> 33)) * _P1) & _M64
            v2 = (v2 + b * _P2) & _M64
            v2 = (((v2 << 31) | (v2 >> 33)) * _P1) & _M64
            v3 = (v3 + c * _P2) & _M64
            v3 = (((v3 << 31) | (v3 >> 33)) * _P1) & _M64
            v4 = (v4 + d * _P2) & _M64
            v4 = (((v4 << 31) | (v4 >> 33)) * _P1) & _M64
        h = (((v1 << 1) | (v1 >> 63)) + ((v2 << 7) | (v2 >> 57)) +
             ((v3 << 12) | (v3 >> 52)) + ((v4 << 18) | (v4 >> 46))) & _M64
        h = _merge(_merge(_merge(_merge(h, v1), v2), v3), v4)
        i = limit
    else:
        h = (seed + _P5) & _M64
    h = (h + n) & _M64
    while i + 8 <= n:
        h ^= _round(0, _Q.unpack_from(data, i)[0])
        h = ((((h << 27) | (h >> 37)) & _M64) * _P1 + _P4) & _M64
        i += 8
    if i + 4 <= n:
        h ^= (_U32.unpack_from(data, i)[0] * _P1) & _M64
        h = ((((h << 23) | (h >> 41)) & _M64) * _P2 + _P3) & _M64
        i += 4
    while i < n:
        h ^= (data[i] * _P5) & _M64
        h = ((((h << 11) | (h >> 53)) & _M64) * _P1) & _M64
        i += 1
    h ^= h >> 33
    h = (h * _P2) & _M64
    h ^= h >> 29
    h = (h * _P3) & _M64
    h ^= h >> 32
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
        raise _Bad("match offset %d reaches before the start of the frame" % dist)
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


# -- bit streams ----------------------------------------------------------------------------
class _Back(object):
    """Backward bit stream data[low:end]: the last byte holds a 1 marking where the stream
    starts; bits are read from just below it towards data[low]. Reading past the beginning
    gives zero bits and counts them in `over`."""

    __slots__ = ("data", "low", "pos", "acc", "n", "over")

    def __init__(self, data, low, end, what):
        if end <= low:
            raise _Bad("empty %s bit stream" % what)
        last = data[end - 1]
        if not last:
            raise _Bad("%s bit stream has no end marker" % what)
        self.data, self.low, self.pos, self.over = data, low, end - 1, 0
        self.n = last.bit_length() - 1
        self.acc = last & ((1 << self.n) - 1)

    def read(self, k):
        if k > self.n:
            m = min((64 - self.n) >> 3, self.pos - self.low)
            if m > 0:
                self.pos -= m
                self.acc = (self.acc << (8 * m)) | \
                    int.from_bytes(self.data[self.pos:self.pos + m], "little")
                self.n += 8 * m
            if k > self.n:
                d = k - self.n
                self.acc <<= d
                self.n += d
                self.over += d
        self.n -= k
        v = self.acc >> self.n
        self.acc &= (1 << self.n) - 1
        return v

    def left(self):
        """Bits not yet read (negative when reading went past the beginning)."""
        return self.n + 8 * (self.pos - self.low) - self.over


# -- FSE ------------------------------------------------------------------------------------
def _read_ncount(data, pos, end, max_sym, max_log, what):
    """FSE table description at data[pos:end]: (normalized counts, accuracy log, position
    after it)."""
    chunk = data[pos:min(end, pos + 1024)]
    avail = 8 * len(chunk)
    if avail < 8:
        raise _Bad("%s table description missing" % what)
    v = int.from_bytes(chunk, "little")
    log = (v & 15) + 5
    if log > max_log:
        raise _Bad("%s table accuracy log %d exceeds %d" % (what, log, max_log))
    bit = 4
    remaining = (1 << log) + 1
    threshold = 1 << log
    nbits = log + 1
    norm = []
    prev0 = False
    while remaining > 1:
        if prev0:
            while True:
                r = (v >> bit) & 3
                bit += 2
                if r:
                    norm.extend([0] * r)
                if len(norm) > max_sym or bit > avail:
                    raise _Bad("%s table description has too many symbols" % what)
                if r != 3:
                    break
        mx = (2 * threshold - 1) - remaining
        low = (v >> bit) & (threshold - 1)
        if low < mx:
            count = low
            bit += nbits - 1
        else:
            count = (v >> bit) & (2 * threshold - 1)
            if count >= threshold:
                count -= mx
            bit += nbits
        if bit > avail:
            raise _Bad("%s table description runs past its section" % what)
        count -= 1
        remaining -= count if count >= 0 else 1
        norm.append(count)
        prev0 = count == 0
        if remaining > 1 and len(norm) > max_sym:
            raise _Bad("%s table description has too many symbols" % what)
        if remaining < threshold:
            nbits = remaining.bit_length()
            threshold = 1 << (nbits - 1)
    if remaining != 1:
        raise _Bad("%s table probabilities do not add up" % what)
    return norm, log, pos + ((bit + 7) >> 3)


def _fse_table(norm, log, what):
    """Decoding table [(symbol, bits, state base), ...] of 2**log states."""
    size = 1 << log
    if sum(c if c > 0 else 1 for c in norm if c) != size:
        raise _Bad("%s table probabilities do not add up" % what)
    syms = [0] * size
    nxt = [0] * len(norm)
    high = size - 1
    for s, c in enumerate(norm):
        if c == -1:
            syms[high] = s
            high -= 1
            nxt[s] = 1
        else:
            nxt[s] = c
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    p = 0
    for s, c in enumerate(norm):
        for _ in range(c):
            syms[p] = s
            p = (p + step) & mask
            while p > high:
                p = (p + step) & mask
    if p != 0:
        raise _Bad("%s table does not spread evenly" % what)
    table = []
    for s in syms:
        x = nxt[s]
        nxt[s] = x + 1
        nb = log - (x.bit_length() - 1)
        table.append((s, nb, (x << nb) - size))
    return table


# Sequence codes: baseline value and extra bits per code.
LL_BASE = tuple(range(16)) + (16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024,
                              2048, 4096, 8192, 16384, 32768, 65536)
LL_BITS = (0,) * 16 + (1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
ML_BASE = tuple(range(3, 35)) + (35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515,
                                 1027, 2051, 4099, 8195, 16387, 32771, 65539)
ML_BITS = (0,) * 32 + (1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
OF_MAX = 31
LL_DEFAULT = (4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2,
              1, 1, 1, 1, 1, -1, -1, -1, -1)
ML_DEFAULT = (1, 4, 3, 2, 2, 2, 2, 2, 2) + (1,) * 37 + (-1,) * 7
OF_DEFAULT = (1, 1, 1, 1, 1, 1, 2, 2, 2) + (1,) * 15 + (-1,) * 5

# (name, max symbol, max accuracy log) for literal lengths, offsets, match lengths
_SEQ_KINDS = (("literal length", 35, 9), ("offset", OF_MAX, 8), ("match length", 52, 9))


def _seq_table(kind, fse):
    """Sequence decoding table: per state (value base, extra bits, extra mask, state bits,
    state mask, next state base)."""
    out = []
    for s, nb, base in fse:
        if kind == 0:
            vb, vx = LL_BASE[s], LL_BITS[s]
        elif kind == 1:
            vb, vx = 1 << s, s
        else:
            vb, vx = ML_BASE[s], ML_BITS[s]
        out.append((vb, vx, (1 << vx) - 1, nb, (1 << nb) - 1, base))
    return out


_PREDEFINED = (
    (_seq_table(0, _fse_table(LL_DEFAULT, 6, "predefined")), 6),
    (_seq_table(1, _fse_table(OF_DEFAULT, 5, "predefined")), 5),
    (_seq_table(2, _fse_table(ML_DEFAULT, 6, "predefined")), 6),
)


# -- Huffman --------------------------------------------------------------------------------
def _fse_weights(data, start, end, table, log):
    """Huffman weights from an FSE-compressed stream with two interleaved states."""
    bits = _Back(data, start, end, "Huffman weight")
    read = bits.read
    s1 = read(log)
    s2 = read(log)
    out = []
    while len(out) < 255:
        sym, nb, base = table[s1]
        out.append(sym)
        s1 = base + read(nb)
        if bits.over:
            out.append(table[s2][0])
            break
        sym, nb, base = table[s2]
        out.append(sym)
        s2 = base + read(nb)
        if bits.over:
            out.append(table[s1][0])
            break
    else:
        raise _Bad("too many Huffman weights")
    if len(out) > 255:
        raise _Bad("too many Huffman weights")
    return out


def _huf_build(weights):
    """Decoding table for the explicit weights (the last symbol's weight is implied):
    (table indexed by the next max_bits bits giving (bits << 8) | symbol, max_bits)."""
    total = 0
    for w in weights:
        if w > HUF_MAX_BITS:
            raise _Bad("Huffman weight %d too large" % w)
        if w:
            total += 1 << (w - 1)
    if not total:
        raise _Bad("Huffman weights are all zero")
    max_bits = total.bit_length()
    if max_bits > HUF_MAX_BITS:
        raise _Bad("Huffman codes longer than %d bits" % HUF_MAX_BITS)
    rest = (1 << max_bits) - total
    if rest & (rest - 1):
        raise _Bad("Huffman weights do not describe a complete code")
    weights = list(weights) + [rest.bit_length()]
    counts = [0] * (max_bits + 1)
    for w in weights:
        if w:
            counts[w] += 1
    starts = [0] * (max_bits + 2)
    acc = 0
    for w in range(1, max_bits + 1):
        starts[w] = acc
        acc += counts[w] << (w - 1)
    table = [0] * (1 << max_bits)
    for sym, w in enumerate(weights):
        if w:
            length = 1 << (w - 1)
            p = starts[w]
            table[p:p + length] = [((max_bits + 1 - w) << 8) | sym] * length
            starts[w] = p + length
    return table, max_bits


def _huf_header(data, pos, end):
    """Huffman tree description at data[pos:end]: (table, max_bits, position after it,
    whether the weights were stored directly)."""
    if pos >= end:
        raise _Bad("Huffman tree description missing")
    head = data[pos]
    pos += 1
    if head >= 128:
        count = head - 127
        size = (count + 1) >> 1
        if pos + size > end:
            raise _Bad("Huffman weights run past the literals section")
        weights = []
        for i in range(count):
            b = data[pos + (i >> 1)]
            weights.append(b & 15 if i & 1 else b >> 4)
        pos += size
    else:
        if pos + head > end or not head:
            raise _Bad("Huffman weights run past the literals section")
        norm, log, p = _read_ncount(data, pos, pos + head, 255, 6, "Huffman weight")
        weights = _fse_weights(data, p, pos + head, _fse_table(norm, log, "Huffman weight"),
                               log)
        pos += head
    table, max_bits = _huf_build(weights)
    return table, max_bits, pos, head >= 128


def _huf_stream(data, start, end, count, table, max_bits, lits):
    """Decode count symbols of the backward Huffman stream data[start:end] into lits."""
    if end <= start:
        raise _Bad("empty Huffman stream")
    last = data[end - 1]
    if not last:
        raise _Bad("Huffman stream has no end marker")
    n = last.bit_length() - 1
    acc = last & ((1 << n) - 1)
    pos = end - 1
    mask = (1 << max_bits) - 1
    append = lits.append
    for _ in range(count):
        if n < max_bits:
            if pos > start:
                m = min(30, pos - start)
                pos -= m
                acc = ((acc & ((1 << n) - 1)) << (m << 3)) | \
                    int.from_bytes(data[pos:pos + m], "little")
                n += m << 3
            if n < max_bits:
                e = table[(acc << (max_bits - n)) & mask]
                if e >> 8 > n:
                    raise _Bad("Huffman stream ends early")
                n -= e >> 8
                append(e & 255)
                continue
        e = table[(acc >> (n - max_bits)) & mask]
        n -= e >> 8
        append(e & 255)
    if n or pos > start:
        raise _Bad("Huffman stream has unused bits")


# -- blocks ---------------------------------------------------------------------------------
class _State(object):
    """What carries from block to block inside one frame."""

    __slots__ = ("huf", "seq", "reps")

    def __init__(self):
        self.huf = None
        self.seq = [None, None, None]
        self.reps = [1, 4, 8]


_LIT_HEAD = {0: (3, 10), 1: (3, 10), 2: (4, 14), 3: (5, 18)}


def _literals(data, pos, end, st, block_max, feats):
    """Literals section at data[pos:end]: (literal bytes, position after the section)."""
    if pos >= end:
        raise _Bad("literals section missing")
    b0 = data[pos]
    ltype, fmt = b0 & 3, (b0 >> 2) & 3
    if ltype < 2:
        if not fmt & 1:
            size, hsize = b0 >> 3, 1
        elif fmt == 1:
            size, hsize = (b0 >> 4) + (data[pos + 1] << 4) if pos + 1 < end else 0, 2
        else:
            size, hsize = ((b0 >> 4) + (data[pos + 1] << 4) + (data[pos + 2] << 12)
                           if pos + 2 < end else 0), 3
        pos += hsize
        if pos > end:
            raise _Bad("literals header runs past the block")
        if size > block_max:
            raise _Bad("%d literals exceed the %d-byte block maximum" % (size, block_max))
        if ltype == 0:
            if pos + size > end:
                raise _Bad("raw literals run past the block")
            feats.add("literals raw")
            return data[pos:pos + size], pos + size
        if pos >= end:
            raise _Bad("RLE literal byte missing")
        feats.add("literals rle")
        return data[pos:pos + 1] * size, pos + 1
    hsize, bits = _LIT_HEAD[fmt]
    if pos + hsize > end:
        raise _Bad("literals header runs past the block")
    h = int.from_bytes(data[pos:pos + hsize], "little")
    regen = (h >> 4) & ((1 << bits) - 1)
    comp = (h >> (4 + bits)) & ((1 << bits) - 1)
    pos += hsize
    if regen > block_max:
        raise _Bad("%d literals exceed the %d-byte block maximum" % (regen, block_max))
    cend = pos + comp
    if cend > end:
        raise _Bad("compressed literals run past the block")
    if ltype == 2:
        table, max_bits, p, direct = _huf_header(data, pos, cend)
        st.huf = (table, max_bits)
        feats.add("huffman direct weights" if direct else "huffman fse weights")
    else:
        if st.huf is None:
            raise _Bad("literals reuse a Huffman table but none was described")
        table, max_bits = st.huf
        p = pos
        feats.add("huffman treeless")
    lits = bytearray()
    if fmt == 0:
        feats.add("huffman 1 stream")
        _huf_stream(data, p, cend, regen, table, max_bits, lits)
    else:
        feats.add("huffman 4 streams")
        if p + 6 > cend:
            raise _Bad("Huffman jump table runs past the literals section")
        s1, s2, s3 = _3H.unpack_from(data, p)
        p += 6
        seg = (regen + 3) >> 2
        last = regen - 3 * seg
        if last < 0:
            raise _Bad("too few literals for four Huffman streams")
        a, b, c = p + s1, p + s1 + s2, p + s1 + s2 + s3
        if c > cend:
            raise _Bad("Huffman streams run past the literals section")
        _huf_stream(data, p, a, seg, table, max_bits, lits)
        _huf_stream(data, a, b, seg, table, max_bits, lits)
        _huf_stream(data, b, c, seg, table, max_bits, lits)
        _huf_stream(data, c, cend, last, table, max_bits, lits)
    return lits, cend


_MODE_NAMES = ("predefined", "rle", "fse", "repeat")


def _code_table(data, pos, end, kind, mode, st, feats):
    """Decoding table (states, log) for one sequence code kind; returns (table, log, pos)."""
    name, max_sym, max_log = _SEQ_KINDS[kind]
    feats.add("sequences " + _MODE_NAMES[mode])
    if mode == 0:
        table, log = _PREDEFINED[kind]
    elif mode == 1:
        if pos >= end:
            raise _Bad("%s RLE code missing" % name)
        sym = data[pos]
        pos += 1
        if sym > max_sym:
            raise _Bad("%s code %d out of range" % (name, sym))
        table, log = _seq_table(kind, [(sym, 0, 0)]), 0
    elif mode == 2:
        norm, log, pos = _read_ncount(data, pos, end, max_sym, max_log, name)
        table = _seq_table(kind, _fse_table(norm, log, name))
    else:
        if st.seq[kind] is None:
            raise _Bad("%s table repeated but none was described" % name)
        table, log = st.seq[kind]
    st.seq[kind] = (table, log)
    return table, log, pos


def _sequences(data, pos, end, lits, out, limit, low, st, block_max, feats):
    """Sequences section at data[pos:end]: decode and execute the sequences, then append the
    remaining literals."""
    if pos >= end:
        raise _Bad("sequences section missing")
    b0 = data[pos]
    if b0 < 128:
        nseq, pos = b0, pos + 1
    elif b0 < 255:
        if pos + 2 > end:
            raise _Bad("sequence count cut off")
        nseq, pos = ((b0 - 128) << 8) + data[pos + 1], pos + 2
    else:
        if pos + 3 > end:
            raise _Bad("sequence count cut off")
        nseq, pos = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00, pos + 3
    if nseq == 0:
        if pos != end:
            raise _Bad("%d bytes after an empty sequences section" % (end - pos))
        _put(out, lits, limit)
        return
    if nseq > block_max // 3:
        raise _Bad("%d sequences cannot fit in a %d-byte block" % (nseq, block_max))
    if pos >= end:
        raise _Bad("sequence compression modes missing")
    modes = data[pos]
    pos += 1
    if modes & 3:
        raise _Bad("reserved bits set in the sequence compression modes")
    ll_t, ll_log, pos = _code_table(data, pos, end, 0, modes >> 6, st, feats)
    of_t, of_log, pos = _code_table(data, pos, end, 1, (modes >> 4) & 3, st, feats)
    ml_t, ml_log, pos = _code_table(data, pos, end, 2, (modes >> 2) & 3, st, feats)

    bits = _Back(data, pos, end, "sequence")
    ls = bits.read(ll_log)
    os_ = bits.read(of_log)
    ms = bits.read(ml_log)
    if bits.over:
        raise _Bad("sequence bit stream ends early")
    start, bp, acc, n = pos, bits.pos, bits.acc, bits.n
    over = 0
    r0, r1, r2 = st.reps
    lp, nlit = 0, len(lits)
    last = nseq - 1
    used_rep = False
    olen = len(out)
    from_bytes = int.from_bytes
    for i in range(nseq):
        if n < 128:
            if bp > start:
                m = min(32, bp - start)
                bp -= m
                acc = ((acc & ((1 << n) - 1)) << (m << 3)) | from_bytes(data[bp:bp + m],
                                                                         "little")
                n += m << 3
            if n < 128:
                acc = (acc & ((1 << n) - 1)) << 128
                n += 128
                over += 128
        ob, ox, om, onb, onm, osb = of_t[os_]
        mb, mx, mm, mnb, mnm, msb = ml_t[ms]
        lb, lx, lm, lnb, lnm, lsb = ll_t[ls]
        n -= ox
        off = ob + ((acc >> n) & om)
        n -= mx
        ml = mb + ((acc >> n) & mm)
        n -= lx
        ll = lb + ((acc >> n) & lm)
        if i != last:
            n -= lnb
            ls = lsb + ((acc >> n) & lnm)
            n -= mnb
            ms = msb + ((acc >> n) & mnm)
            n -= onb
            os_ = osb + ((acc >> n) & onm)
        if over and n < over:
            raise _Bad("sequence bit stream ends early")
        if off > 3:
            off -= 3
            r2, r1, r0 = r1, r0, off
        else:
            used_rep = True
            if ll:
                off -= 1
            if off == 0:
                off = r0
            elif off == 1:
                off = r1
                r1, r0 = r0, off
            elif off == 2:
                off = r2
                r2, r1, r0 = r1, r0, off
            else:
                off = r0 - 1
                if off <= 0:
                    raise _Bad("repeat offset becomes zero")
                r2, r1, r0 = r1, r0, off
        lend = lp + ll
        if lend > nlit:
            raise _Bad("sequences use more literals than the block has")
        if olen + ll + ml <= limit:
            if ll:
                out += lits[lp:lend]
                lp = lend
                olen += ll
            src = olen - off
            if src < low:
                raise _Bad("match offset %d reaches before the start of the frame" % off)
            if ml <= off:
                out += out[src:src + ml]
            else:
                out += (out[src:] * (ml // off + 1))[:ml]
            olen += ml
        else:       # crosses the limit: both helpers stop with _Full
            _put(out, lits[lp:lend], limit)
            lp = lend
            _copy_match(out, off, ml, limit, low)
    left = n - over if over else n + 8 * (bp - start)
    if left:
        raise _Bad("sequence bit stream has %d unused bits" % left)
    st.reps = [r0, r1, r2]
    if used_rep:
        feats.add("repeat offsets")
    if lp < nlit:
        _put(out, lits[lp:], limit)


def _compressed_block(data, pos, end, out, limit, low, st, block_max, feats):
    lits, pos = _literals(data, pos, end, st, block_max, feats)
    _sequences(data, pos, end, lits, out, limit, low, st, block_max, feats)


# -- frames ---------------------------------------------------------------------------------
_DID_SIZE = (0, 1, 2, 4)


def _frame(data, pos, out, cap, info):
    """One Zstandard frame whose header starts at pos (just after the magic). Returns the
    position after the frame."""
    n = len(data)
    feats = info["features"]
    if pos >= n:
        raise _Short("frame header cut off")
    fhd = data[pos]
    pos += 1
    if fhd & 0x08:
        raise _Bad("reserved bit set in the frame header")
    single = fhd & 0x20
    window = None
    if not single:
        if pos >= n:
            raise _Short("frame header cut off")
        wd = data[pos]
        pos += 1
        base = 1 << (10 + (wd >> 3))
        window = base + (base >> 3) * (wd & 7)
    did_size = _DID_SIZE[fhd & 3]
    fcs_size = (1 if single else 0, 2, 4, 8)[fhd >> 6]
    if pos + did_size + fcs_size > n:
        raise _Short("frame header cut off")
    did = int.from_bytes(data[pos:pos + did_size], "little")
    pos += did_size
    fcs = None
    if fcs_size:
        fcs = int.from_bytes(data[pos:pos + fcs_size], "little")
        if fcs_size == 2:
            fcs += 256
        pos += fcs_size
    if did:
        info["dictionary"] = did
        raise _Bad("frame needs dictionary %d, which is not available" % did)
    if single:
        window = fcs
    if window > LARGE_WINDOW:
        info["notes"].append("frame declares a %d-byte window" % window)
    block_max = min(window, BLOCK_SIZE_MAX)
    start = len(out)
    st = _State()
    while True:
        if pos + 3 > n:
            raise _Short("block header cut off")
        h = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)
        pos += 3
        last, btype, size = h & 1, (h >> 1) & 3, h >> 3
        info["blocks"] += 1
        if btype == 3:
            raise _Bad("reserved block type")
        if size > block_max:
            raise _Bad("block of %d bytes exceeds the frame's %d-byte maximum"
                       % (size, block_max))
        if btype == 0:
            feats.add("raw block")
            _put(out, data[pos:pos + size], cap)
            if pos + size > n:
                raise _Short("raw block cut off")
            pos += size
        elif btype == 1:
            feats.add("rle block")
            if pos >= n:
                raise _Short("RLE block cut off")
            _put(out, data[pos:pos + 1] * size, cap)
            pos += 1
        else:
            feats.add("compressed block")
            if pos + size > n:
                raise _Short("compressed block cut off")
            limit = min(cap, len(out) + block_max)
            try:
                _compressed_block(data, pos, pos + size, out, limit, start, st, block_max,
                                  feats)
            except _Full:
                if len(out) >= cap:
                    raise
                raise _Bad("block decodes to more than the %d-byte block maximum"
                           % block_max)
            pos += size
        if last:
            break
    produced = len(out) - start
    if fhd & 0x04:
        if pos + 4 > n:
            raise _Short("content checksum cut off")
        stored = _U32.unpack_from(data, pos)[0]
        pos += 4
        feats.add("checksum")
        if produced > info["checksum_bytes"]:
            info["notes"].append("content checksum not verified (output over limit "
                                 "decode_checksum_bytes)")
        elif xxh64(out[start:]) & 0xFFFFFFFF != stored:
            info["problems"].append("content checksum mismatch")
        else:
            info["verified"] = True
    if fcs is not None and fcs != produced:
        info["problems"].append("frame declares %d bytes of content, decoded %d"
                                % (fcs, produced))
    return pos


def _frames(data, out, cap, info, max_frames):
    pos, n = 0, len(data)
    if n < 4:
        raise _Short("too short for a Zstandard frame")
    for i in range(max_frames):
        if pos + 4 > n:
            break
        magic = _U32.unpack_from(data, pos)[0]
        if 0x184D2A50 <= magic <= 0x184D2A5F:
            if pos + 8 > n:
                raise _Short("skippable frame header cut off")
            size = _U32.unpack_from(data, pos + 4)[0]
            if pos + 8 + size > n:
                raise _Short("skippable frame cut off")
            pos += 8 + size
            info["features"].add("skippable frame")
            info["notes"].append("skippable frame of %d bytes" % size)
            continue
        if magic != MAGIC:
            if i == 0:
                raise _Bad("not a Zstandard frame")
            break
        pos = _frame(data, pos + 4, out, cap, info)
        info["frames"] += 1
    else:
        if pos + 4 <= n:
            magic = _U32.unpack_from(data, pos)[0]
            if magic == MAGIC or 0x184D2A50 <= magic <= 0x184D2A5F:
                info["notes"].append("stopped after %d frames" % max_frames)
    return pos


# -- attempt --------------------------------------------------------------------------------
def decompress(data, cap, max_frames=MAX_FRAMES, checksum_bytes=MAX_CHECKSUM_BYTES):
    """(output bytes, status, reason, end position, info) for a Zstandard buffer.

    status is "ok", "capped", "truncated" or "corrupt"; reason explains the last two. Output
    stops at cap bytes. info has "frames", "blocks", "notes", "problems", "verified" (a
    content checksum matched) and "features" (the format features met). Never raises.
    """
    out = bytearray()
    info = {"frames": 0, "blocks": 0, "notes": [], "problems": [], "verified": False,
            "features": set(), "checksum_bytes": checksum_bytes}
    status, reason, end = "ok", "", 0
    try:
        data = memoryview(data).tobytes()
        end = len(data)
        end = _frames(data, out, cap, info, max_frames)
    except _Full:
        status = "capped"
    except _Short as e:
        status, reason = "truncated", str(e)
    except _Bad as e:
        status, reason = "corrupt", str(e)
    except Exception as e:      # noqa: BLE001 - any other slip is malformed input
        status, reason = "corrupt", "malformed data (%s)" % type(e).__name__
    if len(out) > cap:
        del out[max(cap, 0):]
    return bytes(out), status, reason, end, info
