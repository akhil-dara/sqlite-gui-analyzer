"""LZ4 (frame and Apple block streams), LZVN and LZFSE decoding.

Every compressed vector is built here by small test-side encoders (LZ4 sequences, LZVN
opcodes, an FSE encoder for LZFSE v1/v2 blocks), and expected output comes from simple
byte-by-byte reference executors, independent of the decoder's copy tricks.
"""
import random
import struct
import time
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests import decode_samples as S
from engine.decode import decode_blob, interpretations
from engine.decode import detect, lz
from engine.decode.core import Context


# -- reference helpers --------------------------------------------------------------------
def slow_xxh32(data, seed=0):
    """Straightforward xxHash32 written from the algorithm description, lane by lane."""
    p1, p2, p3, p4, p5 = 2654435761, 2246822519, 3266489917, 668265263, 374761393
    mask = (1 << 32) - 1

    def rotl(x, r):
        return ((x << r) | (x >> (32 - r))) & mask

    def lane(i):
        return data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)

    n, i = len(data), 0
    if n >= 16:
        acc = [(seed + p1 + p2) % (1 << 32), (seed + p2) % (1 << 32), seed % (1 << 32),
               (seed - p1) % (1 << 32)]
        while i + 16 <= n:
            for j in range(4):
                acc[j] = rotl((acc[j] + lane(i + 4 * j) * p2) % (1 << 32), 13) * p1 % (1 << 32)
            i += 16
        h = (rotl(acc[0], 1) + rotl(acc[1], 7) + rotl(acc[2], 12) + rotl(acc[3], 18)) & mask
    else:
        h = (seed + p5) & mask
    h = (h + n) & mask
    while i + 4 <= n:
        h = rotl((h + lane(i) * p3) & mask, 17) * p4 & mask
        i += 4
    while i < n:
        h = rotl((h + data[i] * p5) & mask, 11) * p1 & mask
        i += 1
    h = ((h ^ (h >> 15)) * p2) & mask
    h = ((h ^ (h >> 13)) * p3) & mask
    return h ^ (h >> 16)


def execute(ops, out=None):
    """Reference LZ77 executor: ops are (literals, match length, distance)."""
    out = bytearray() if out is None else out
    for lits, mlen, dist in ops:
        out += lits
        for _ in range(mlen):
            out.append(out[-dist])
    return bytes(out)


def ext_bytes(n):
    out = bytearray()
    while n >= 255:
        out.append(255)
        n -= 255
    out.append(n)
    return bytes(out)


def lz4_seq(lits, dist=None, mlen=None):
    ml = None if dist is None else mlen - 4
    out = bytearray([(min(len(lits), 15) << 4) | (0 if ml is None else min(ml, 15))])
    if len(lits) >= 15:
        out += ext_bytes(len(lits) - 15)
    out += lits
    if dist is not None:
        out += struct.pack("<H", dist)
        if ml >= 15:
            out += ext_bytes(ml - 15)
    return bytes(out)


def lz4_block(ops):
    """Encode ops (last one literal-only) as an LZ4 block; returns (block, expected)."""
    block = b"".join(lz4_seq(l, d, m) if m else lz4_seq(l) for l, m, d in ops)
    return block, execute(ops)


LZ4_OPS = [(b"Hello, LZ4 world! ", 36, 18),        # overlapping: copies itself twice
           (b"x", 40, 1),                           # run of 'x', length needs extension
           (b"ABCDEFGHIJKLMNOPQRST", 4, 5),          # 20 literals: literal-length extension
           (b"", 300, 60),                          # long match, two extension bytes
           (b"end.", 0, 0)]


def lz4_frame(blocks, block_sums=False, content=None, content_sum=False, independent=True,
              bd=0x40):
    """blocks: [(payload, stored)]; content: decoded bytes (for size and checksum)."""
    flg = 0x40 | (0x20 if independent else 0) | (0x10 if block_sums else 0) | \
        (0x08 if content is not None else 0) | (0x04 if content_sum else 0)
    desc = bytes([flg, bd]) + (struct.pack("<Q", len(content)) if content is not None else b"")
    out = bytearray(b"\x04\x22\x4d\x18" + desc + bytes([(lz.xxh32(desc) >> 8) & 0xFF]))
    for payload, stored in blocks:
        out += struct.pack("<I", len(payload) | (0x80000000 if stored else 0)) + payload
        if block_sums:
            out += struct.pack("<I", lz.xxh32(payload))
    out += b"\x00\x00\x00\x00"
    if content_sum:
        out += struct.pack("<I", lz.xxh32(content))
    return bytes(out)


# -- LZVN test encoder ---------------------------------------------------------------------
def vn_sml_l(lits):
    return bytes([0xE0 | len(lits)]) + lits


def vn_lrg_l(lits):
    return bytes([0xE0, len(lits) - 16]) + lits


def vn_sml_d(lits, m, d):
    assert d < 1536 and 3 <= m <= 10 and len(lits) <= 3
    return bytes([(len(lits) << 6) | ((m - 3) << 3) | (d >> 8), d & 0xFF]) + lits


def vn_med_d(lits, m, d):
    word = (d << 2) | ((m - 3) & 3)
    return bytes([0xA0 | (len(lits) << 3) | ((m - 3) >> 2)]) + struct.pack("<H", word) + lits


def vn_lrg_d(lits, m, d):
    return bytes([(len(lits) << 6) | ((m - 3) << 3) | 7]) + struct.pack("<H", d) + lits


def vn_pre_d(lits, m):
    assert 1 <= len(lits) <= 3
    return bytes([(len(lits) << 6) | ((m - 3) << 3) | 6]) + lits


def vn_sml_m(m):
    return bytes([0xF0 | m])


def vn_lrg_m(m):
    return bytes([0xF0, m - 16])


VN_EOS = b"\x06" + b"\x00" * 7
VN_NOP = b"\x0e"


def lzvn_vector():
    """(payload, expected) exercising every opcode class."""
    long_lits = bytes(range(32, 127)) * 2 + b"0123456789"             # 200 bytes
    parts = [(vn_lrg_l(long_lits), (long_lits, 0, 0)),
             (vn_sml_l(b"abcdefgh"), (b"abcdefgh", 0, 0)),
             (vn_sml_d(b"XY", 5, 4), (b"XY", 5, 4)),
             (VN_NOP, (b"", 0, 0)),
             (vn_med_d(b"Z", 20, 12), (b"Z", 20, 12)),
             (vn_lrg_d(b"", 10, 230), (b"", 10, 230)),
             (vn_pre_d(b"pqr", 4), (b"pqr", 4, 230)),
             (vn_sml_m(7), (b"", 7, 230)),
             (vn_lrg_m(40), (b"", 40, 230)),
             (vn_sml_d(b"", 9, 1), (b"", 9, 1)),
             (vn_sml_l(b"!"), (b"!", 0, 0))]
    payload = b"".join(p for p, _ in parts) + VN_EOS
    return payload, execute([op for _, op in parts])


# -- LZFSE test encoder --------------------------------------------------------------------
def bases(extra):
    b = [0]
    for e in extra[:-1]:
        b.append(b[-1] + (1 << e))
    return b


L_EXTRA = [0] * 16 + [2, 3, 5, 8]
M_EXTRA = [0] * 16 + [3, 5, 8, 11]
D_EXTRA = [i // 4 for i in range(64)]
L_BASE, M_BASE, D_BASE = bases(L_EXTRA), bases(M_EXTRA), bases(D_EXTRA)


def normalize(symbols, nsymbols, nstates):
    counts = [0] * nsymbols
    for s in symbols:
        counts[s] += 1
    total = sum(counts)
    freq = [0] * nsymbols
    if not total:
        return freq
    for s, c in enumerate(counts):
        if c:
            freq[s] = max(1, c * nstates // total)
    while sum(freq) != nstates:
        big = max(range(nsymbols), key=lambda s: freq[s])
        if sum(freq) < nstates:
            freq[big] += 1
        else:
            freq[big] -= 1
    return freq


def fse_entries(nstates, freq):
    """Per symbol: [(state, k, delta)], the decoder table of the format description."""
    out, state, top = {}, 0, nstates.bit_length()
    for sym, f in enumerate(freq):
        if not f:
            continue
        k = top - f.bit_length()
        j0 = ((2 * nstates) >> k) - f
        rows = []
        for j in range(f):
            if j < j0:
                rows.append((state, k, ((f + j) << k) - nstates))
            else:
                rows.append((state, k - 1, (j - j0) << (k - 1)))
            state += 1
        out[sym] = rows
    return out


def fse_encode(symbols, nstates, freq, extras=None):
    """Encode one state machine backwards: (initial state, [(width, bits)] in read order).
    extras: per symbol (value bits, extra value) for L/M/D streams."""
    table = fse_entries(nstates, freq)
    nxt, fields = 0, []
    for i in range(len(symbols) - 1, -1, -1):
        for state, k, delta in table[symbols[i]]:
            if delta <= nxt < delta + (1 << k):
                break
        else:
            raise AssertionError("no state interval")
        width, bits = k, nxt - delta
        if extras is not None:
            vbits, extra = extras[i]
            width, bits = width + vbits, (bits << vbits) | extra
        fields.append((width, bits))
        nxt = state
    fields.reverse()
    return nxt, fields


def pack_bits(fields):
    """Fields in read order -> (payload, padding bits in [-7, 0])."""
    acc, total = 0, 0
    for width, bits in fields:
        acc = (acc << width) | bits
        total += width
    size = (total + 7) // 8
    return acc.to_bytes(size, "little"), total - 8 * size


def value_symbol(v, base, extra):
    sym = max(i for i in range(len(base)) if base[i] <= v)
    assert v - base[sym] < (1 << extra[sym])
    return sym, (extra[sym], v - base[sym])


def greedy_lmd(data):
    """Tiny greedy LZ77 parse: [(literals, match length, distance)], last has no match."""
    ops, i, lit_start = [], 0, 0
    while i < len(data):
        best, best_d = 0, 0
        for d in range(1, min(i, 3000) + 1):
            n = 0
            while i + n < len(data) and data[i + n] == data[i + n - d] and n < 2000:
                n += 1
            if n > best:
                best, best_d = n, d
        if best >= 4 and i - lit_start <= 315:
            ops.append((data[lit_start:i], best, best_d))
            i += best
            lit_start = i
        else:
            i += 1
    ops.append((data[lit_start:], 0, 0))
    return ops


def lzfse_block(data, version=2):
    ops = greedy_lmd(data)
    lits = b"".join(l for l, _, _ in ops)
    lits_padded = lits + b"\x00" * (-len(lits) % 4)
    lit_freq = normalize(lits_padded, 256, 1024)
    streams = [fse_encode(list(lits_padded[q::4]), 1024, lit_freq) for q in range(4)]
    lit_fields = [streams[i % 4][1][i // 4] for i in range(len(lits_padded))]
    lit_payload, lit_bits = pack_bits(lit_fields)
    lsym, msym, dsym, lext, mext, dext = [], [], [], [], [], []
    prev = None
    for l, m, d in ops:
        dv = 0 if (m == 0 or d == prev) else d
        if m:
            prev = d
        for v, base, extra, syms, exts in ((len(l), L_BASE, L_EXTRA, lsym, lext),
                                          (m, M_BASE, M_EXTRA, msym, mext),
                                          (dv, D_BASE, D_EXTRA, dsym, dext)):
            s, e = value_symbol(v, base, extra)
            syms.append(s)
            exts.append(e)
    lf, mf, df = normalize(lsym, 20, 64), normalize(msym, 20, 64), normalize(dsym, 64, 256)
    ls, lfld = fse_encode(lsym, 64, lf, lext)
    ms, mfld = fse_encode(msym, 64, mf, mext)
    ds, dfld = fse_encode(dsym, 256, df, dext)
    lmd_fields = []
    for i in range(len(ops)):
        lmd_fields += [lfld[i], mfld[i], dfld[i]]
    lmd_payload, lmd_bits = pack_bits(lmd_fields)
    lit_states = [s for s, _ in streams]
    freqs = lf + mf + df + lit_freq
    if version == 1:
        header = struct.pack("<4s6Ii4Hi3H360H", b"bvx1", len(data),
                             len(lit_payload) + len(lmd_payload), len(lits_padded), len(ops),
                             len(lit_payload), len(lmd_payload), lit_bits, *(lit_states + [
                                 lmd_bits, ls, ms, ds] + freqs)) + b"\x00\x00"
    else:
        codes = encode_freqs(freqs)
        v0 = len(lits_padded) | (len(lit_payload) << 20) | (len(ops) << 40) | \
            ((lit_bits + 7) << 60)
        v1 = lit_states[0] | (lit_states[1] << 10) | (lit_states[2] << 20) | \
            (lit_states[3] << 30) | (len(lmd_payload) << 40) | ((lmd_bits + 7) << 60)
        v2 = (32 + len(codes)) | (ls << 32) | (ms << 42) | (ds << 52)
        header = b"bvx2" + struct.pack("<I3Q", len(data), v0, v1, v2) + codes
    return header + lit_payload + lmd_payload


def encode_freqs(freqs):
    acc, nbits = 0, 0
    for v in freqs:
        if v <= 3:
            width, code = {0: (2, 0), 1: (2, 2), 2: (3, 1), 3: (3, 5)}[v]
        elif v <= 7:
            width, code = 5, 3 | ((v - 4) << 3)
        elif v <= 23:
            width, code = 8, 7 | ((v - 8) << 4)
        else:
            width, code = 14, 15 | ((v - 24) << 4)
        acc |= code << nbits
        nbits += width
    return acc.to_bytes((nbits + 7) // 8, "little")


LZFSE_TEXT = (b"The quick brown fox jumps over the lazy dog. " * 6 +
              b"Pack my box with five dozen liquor jugs! " * 5 +
              bytes(range(256)) + b"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz" + b"The quick brown")


def run_decode(data, kind, **limits):
    ctx = Context(**limits)
    return lz.decode(data, ctx, 0, kind), ctx


# -- tests ---------------------------------------------------------------------------------
class XxHashTest(unittest.TestCase):
    def test_known_answers(self):
        self.assertEqual(lz.xxh32(b""), 0x02CC5D05)
        self.assertEqual(lz.xxh32(b"a"), 0x550D7456)
        self.assertEqual(lz.xxh32(b"abc"), 0x32D153FF)
        self.assertEqual(slow_xxh32(b""), 0x02CC5D05)
        self.assertEqual(slow_xxh32(b"abc"), 0x32D153FF)

    def test_matches_reference_at_every_length(self):
        rnd = random.Random(7)
        data = bytes(rnd.getrandbits(8) for _ in range(300))
        for n in list(range(0, 70)) + [255, 256, 300]:
            for seed in (0, 1, 0x9E3779B1):
                self.assertEqual(lz.xxh32(data[:n], seed), slow_xxh32(data[:n], seed), (n, seed))


class Lz4Test(unittest.TestCase):
    def test_detection(self):
        self.assertEqual(detect.magic(b"\x04\x22\x4d\x18\x40\x40\xc0")[0], "lz4")
        self.assertEqual(detect.magic(b"bv41....")[0], "lz4_apple")
        self.assertEqual(detect.magic(b"bv4-....")[0], "lz4_apple")
        for tag in (b"bvx2", b"bvx1", b"bvx-"):
            self.assertEqual(detect.magic(tag + b"....")[0], "lzfse")
        self.assertEqual(detect.magic(b"bvxn....")[0], "lzvn")

    def test_raw_block(self):
        block, expected = lz4_block(LZ4_OPS)
        out = bytearray()
        lz.lz4_block(block, 0, len(block), out, 1 << 20)
        self.assertEqual(bytes(out), expected)

    def test_frame_with_checksums_and_size(self):
        block, expected = lz4_block(LZ4_OPS)
        frame = lz4_frame([(block, False)], block_sums=True, content=expected, content_sum=True)
        attempt, ctx = run_decode(frame, "lz4")
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertEqual(attempt.node.children[0].value, expected)
        self.assertEqual(ctx.output_left, 128 * (1 << 20) - len(expected))

    def test_stored_block_and_protobuf_chain(self):
        frame = lz4_frame([(S.PROTOBUF[:10], True), (S.PROTOBUF[10:], True)],
                          content=S.PROTOBUF, content_sum=True, independent=False)
        root = decode_blob(frame)
        node = root.children[0]
        self.assertEqual(node.kind, "lz4")
        self.assertEqual(node.confidence, "confident")
        self.assertEqual(node.children[0].value, S.PROTOBUF)
        self.assertEqual(node.children[0].children[0].kind, "protobuf")

    def test_linked_blocks_reach_back_and_independent_ones_do_not(self):
        first, expected = lz4_block([(b"linked block text ", 0, 0)])
        second = lz4_seq(b"", 18, 18) + lz4_seq(b"!")
        linked = lz4_frame([(first, False), (second, False)], independent=False)
        attempt, _ = run_decode(linked, "lz4")
        self.assertEqual(attempt.node.children[0].value, expected * 2 + b"!")
        self.assertEqual(attempt.confidence, "confident")
        indep = lz4_frame([(first, False), (second, False)], independent=True)
        attempt, _ = run_decode(indep, "lz4")
        self.assertEqual(attempt.confidence, "uncertain")
        self.assertIn("corrupt data", attempt.node.note)

    def test_concatenated_and_skippable_frames(self):
        a = lz4_frame([(b"first ", True)])
        skip = struct.pack("<II", 0x184D2A53, 3) + b"xyz"
        b = lz4_frame([(b"second", True)])
        attempt, _ = run_decode(a + skip + b + b"\x00\x00", "lz4")
        self.assertEqual(attempt.node.children[0].value, b"first second")
        self.assertIn("2 frames", attempt.node.note)
        self.assertIn("skippable", attempt.node.note)

    def test_bad_checksums_reported(self):
        block, expected = lz4_block(LZ4_OPS)
        frame = bytearray(lz4_frame([(block, False)], block_sums=True, content=expected,
                                    content_sum=True))
        frame[-1] ^= 0xFF                   # content checksum
        attempt, _ = run_decode(bytes(frame), "lz4")
        self.assertEqual(attempt.confidence, "uncertain")
        self.assertIn("content checksum mismatch", attempt.node.note)
        self.assertEqual(attempt.node.children[0].value, expected)
        frame[-1] ^= 0xFF
        frame[-9] ^= 0xFF                   # block checksum (before end mark + content sum)
        attempt, _ = run_decode(bytes(frame), "lz4")
        self.assertIn("block 1 checksum mismatch", attempt.node.note)
        frame[-9] ^= 0xFF
        frame[14] ^= 0xFF                   # header checksum byte
        attempt, _ = run_decode(bytes(frame), "lz4")
        self.assertIn("header checksum mismatch", attempt.node.note)

    def test_truncation_never_raises(self):
        block, expected = lz4_block(LZ4_OPS)
        frame = lz4_frame([(block, False)], block_sums=True, content=expected, content_sum=True)
        for n in range(4, len(frame)):
            attempt, _ = run_decode(frame[:n], "lz4")
            if attempt.confidence != "failed":
                self.assertEqual(attempt.confidence, "uncertain", n)
                self.assertIn("ends early", attempt.node.note)
                self.assertTrue(expected.startswith(attempt.node.children[0].value))

    def test_apple_blocks(self):
        block, expected = lz4_block(LZ4_OPS)
        stored = b"--stored--"
        tail = lz4_seq(b"", 10, 10) + lz4_seq(b".")       # reaches into the stored block
        data = (b"bv41" + struct.pack("<II", len(expected), len(block)) + block +
                b"bv4-" + struct.pack("<I", len(stored)) + stored +
                b"bv41" + struct.pack("<II", 11, len(tail)) + tail + b"bv4$")
        attempt, _ = run_decode(data, "lz4_apple")
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertEqual(attempt.node.children[0].value, expected + stored * 2 + b".")
        self.assertIn("3 blocks", attempt.node.note)
        node = decode_blob(data).children[0]
        self.assertEqual(node.kind, "lz4_apple")
        # Without the end marker: output kept, marked uncertain.
        attempt, _ = run_decode(data[:-4], "lz4_apple")
        self.assertEqual(attempt.confidence, "uncertain")
        self.assertIn("bv4$", attempt.node.note)

    def test_bomb_is_capped(self):
        block = lz4_seq(b"a", 1, 4 + 15 + 255 * 4000) + lz4_seq(b"")
        frame = lz4_frame([(block, False)])
        attempt, ctx = run_decode(frame, "lz4", max_output=1 << 16)
        self.assertEqual(attempt.confidence, "confident")
        self.assertEqual(len(attempt.node.children[0].value), 1 << 16)
        self.assertIn("output capped", attempt.node.note)
        self.assertIn("output", ctx.limits_hit)


class LzvnTest(unittest.TestCase):
    def test_every_opcode_class(self):
        payload, expected = lzvn_vector()
        data = b"bvxn" + struct.pack("<II", len(expected), len(payload)) + payload + b"bvx$"
        attempt, _ = run_decode(data, "lzvn")
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertEqual(attempt.node.children[0].value, expected)
        self.assertEqual(decode_blob(data).children[0].kind, "lzvn")

    def test_undefined_opcode_and_bad_distance(self):
        for payload in (vn_sml_l(b"abc") + b"\x70" + VN_EOS,       # undefined opcode
                        vn_sml_l(b"abc") + vn_sml_d(b"", 4, 9) + VN_EOS,   # too far back
                        vn_sml_m(4) + VN_EOS):                      # no previous distance
            data = b"bvxn" + struct.pack("<II", 7, len(payload)) + payload + b"bvx$"
            attempt, _ = run_decode(data, "lzvn")
            self.assertIn(attempt.confidence, ("uncertain", "failed"))

    def test_truncation(self):
        payload, expected = lzvn_vector()
        data = b"bvxn" + struct.pack("<II", len(expected), len(payload)) + payload + b"bvx$"
        for n in range(4, len(data)):
            attempt, _ = run_decode(data[:n], "lzvn")
            if attempt.confidence != "failed":
                self.assertEqual(attempt.confidence, "uncertain", n)
                self.assertTrue(expected.startswith(attempt.node.children[0].value), n)


class LzfseTest(unittest.TestCase):
    def test_v2_block(self):
        data = lzfse_block(LZFSE_TEXT, 2) + b"bvx$"
        attempt, _ = run_decode(data, "lzfse")
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertEqual(attempt.node.children[0].value, LZFSE_TEXT)
        root = decode_blob(data)
        self.assertEqual(root.children[0].kind, "lzfse")
        self.assertEqual(root.children[0].confidence, "confident")

    def test_v1_block(self):
        data = lzfse_block(LZFSE_TEXT, 1) + b"bvx$"
        attempt, _ = run_decode(data, "lzfse")
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertEqual(attempt.node.children[0].value, LZFSE_TEXT)

    def test_small_and_literal_only_blocks(self):
        for text in (b"a", b"abcd", b"hello", b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                     bytes(range(200)), S.PROTOBUF):
            data = lzfse_block(text, 2) + b"bvx$"
            attempt, _ = run_decode(data, "lzfse")
            self.assertEqual(attempt.node.children[0].value, text)
            self.assertEqual(attempt.confidence, "confident")

    def test_mixed_blocks(self):
        payload, expected = lzvn_vector()
        data = (b"bvx-" + struct.pack("<I", 5) + b"raw: " +
                lzfse_block(LZFSE_TEXT, 2) +
                b"bvxn" + struct.pack("<II", len(expected), len(payload)) + payload +
                b"bvx$")
        attempt, _ = run_decode(data, "lzfse")
        self.assertEqual(attempt.node.children[0].value, b"raw: " + LZFSE_TEXT + expected)
        self.assertEqual(attempt.confidence, "confident", attempt.reason)
        self.assertIn("3 blocks", attempt.node.note)

    def test_stored_only(self):
        data = b"bvx-" + struct.pack("<I", 11) + b"hello world" + b"bvx$"
        self.assertEqual(decode_blob(data).children[0].children[0].value, b"hello world")

    def test_corrupt_payload_and_truncation(self):
        data = lzfse_block(LZFSE_TEXT, 2) + b"bvx$"
        for n in range(4, len(data), 7):
            attempt, _ = run_decode(data[:n], "lzfse")
            self.assertIn(attempt.confidence, ("failed", "uncertain"), n)
        rnd = random.Random(3)
        for _ in range(60):
            bad = bytearray(data)
            bad[rnd.randrange(8, len(bad) - 4)] ^= 1 << rnd.randrange(8)
            attempt, _ = run_decode(bytes(bad), "lzfse")     # must not raise
            self.assertIn(attempt.confidence, ("failed", "uncertain", "confident"))


class HostileInputTest(unittest.TestCase):
    MAGICS = ((b"\x04\x22\x4d\x18", "lz4"), (b"bv41", "lz4_apple"), (b"bv4-", "lz4_apple"),
              (b"bvx2", "lzfse"), (b"bvx1", "lzfse"), (b"bvx-", "lzfse"), (b"bvxn", "lzvn"))

    def test_random_bytes_after_magic(self):
        rnd = random.Random(11)
        start = time.time()
        for magic, kind in self.MAGICS:
            for size in (0, 1, 3, 8, 12, 40, 200, 2000):
                for _ in range(15):
                    data = magic + bytes(rnd.getrandbits(8) for _ in range(size))
                    self.assertEqual(detect.magic(data)[0], kind)
                    root = decode_blob(data)                    # never raises
                    self.assertEqual(root.kind, "bytes")
                    self.assertNotIn("error", root.note)
                    interpretations(data)
        self.assertLess(time.time() - start, 60)

    def test_absurd_declared_sizes(self):
        big = b"\xff\xff\xff\xff"
        cases = [b"bvx-" + big + b"abc" + b"bvx$",
                 b"bvxn" + big + big + b"\xe3abc" + VN_EOS,
                 b"bv41" + big + big + lz4_seq(b"abc"),
                 b"bv4-" + big + b"abc",
                 b"\x04\x22\x4d\x18\x60\x40\x82" + b"\xff\xff\x00\x00" + b"abc",
                 b"\x04\x22\x4d\x18\x60\x70\x73" + b"\xff\xff\xff\x7f" + b"abc",
                 b"bvx2" + big + b"\xff" * 24 + b"\x00" * 16]
        for data in cases:
            start = time.time()
            for kind in ("lz4", "lz4_apple", "lzfse"):
                run_decode(data, kind)
            decode_blob(data)
            self.assertLess(time.time() - start, 5, data)

    def test_budget_used_up(self):
        frame = lz4_frame([(b"abc", True)])
        ctx = Context(max_total_output=0)
        attempt = lz.decode(frame, ctx, 0, "lz4")
        self.assertEqual(attempt.confidence, "failed")
        self.assertIn("output", ctx.limits_hit)


if __name__ == "__main__":
    unittest.main()
