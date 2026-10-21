"""Finding records in raw page bytes: intact cells and cells whose first bytes are overwritten.

Table-leaf cell:  payload-length varint, rowid varint, record (header-length varint, serial
                  type varints, body), [4-byte first overflow page]
Index cell:       [4-byte child page (interior only)], payload-length varint, record, [overflow]
(WITHOUT ROWID rows are index cells.)

A cell is only accepted when its record header parses and header length + the body sizes of
its serial types equal the payload length exactly, and the cell fits the space it was found in.

When SQLite frees a cell inside a page it writes a 4-byte freeblock header (next freeblock,
size) over the cell's first 4 bytes: the payload length, the rowid, the header length and the
first serial type(s) are lost. reconstruct() rebuilds them from the bytes that survive, the
exact size of the space the cell occupied, and a table's schema (column count, affinities):
every combination of varint lengths is tried and each lost serial type is solved from the
remaining size.
"""

import struct

from ..fileformat.btree import local_payload_size
from ..fileformat.record import serial_size, to_signed64

MAX_COLUMNS = 2000          # SQLite's default SQLITE_MAX_COLUMN
_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


def varint(buf, pos, end):
    """(value, next_pos) of the minimal varint at buf[pos] ending before `end`, else None."""
    if pos >= end:
        return None
    b = buf[pos]
    if b < 0x80:
        return b, pos + 1
    if b == 0x80:
        return None                 # SQLite always writes minimal varints
    value = b & 0x7F
    p = pos + 1
    for _ in range(7):
        if p >= end:
            return None
        b = buf[p]
        p += 1
        value = (value << 7) | (b & 0x7F)
        if b < 0x80:
            return value, p
    if p >= end:
        return None
    return (value << 8) | buf[p], p + 1


def varint_len(v):
    if v < 0:
        return 9
    n = 1
    while v >= 0x80 and n < 9:
        v >>= 7
        n += 1
    return n


class Cell(object):
    """A parsed (or reconstructed) cell.

    start: offset of the cell; pstart: offset of the record header (header-length varint);
    body: offset of the record body; end: offset after the cell's local bytes (and overflow
    pointer). rowid is None when unknown or not applicable. rebuilt = number of leading bytes
    that were reconstructed (0: intact). lost = number of leading serial types that were
    overwritten and chosen to fit; solved = those of them whose text/blob size was inferred
    from the space. ovfl = first overflow page (None: payload is all local).
    """
    __slots__ = ("start", "index", "payload_len", "rowid", "pstart", "header_len", "types",
                 "body", "local", "end", "ovfl", "rebuilt", "solved", "lost")

    def __init__(self, start, index, payload_len, rowid, pstart, header_len, types, body,
                 local, end, ovfl, rebuilt=0, solved=(), lost=0):
        self.start, self.index, self.payload_len, self.rowid = start, index, payload_len, rowid
        self.pstart, self.header_len, self.types, self.body = pstart, header_len, types, body
        self.local, self.end, self.ovfl = local, end, ovfl
        self.rebuilt, self.solved, self.lost = rebuilt, tuple(solved), lost

    @property
    def body_len(self):
        return self.payload_len - self.header_len

    def local_body(self, buf):
        """Body bytes stored on the page itself."""
        return bytes(buf[self.body:self.pstart + self.local])

    def __repr__(self):
        return "Cell(@%d-%d P=%d rowid=%r types=%r rebuilt=%d)" % (
            self.start, self.end, self.payload_len, self.rowid, self.types, self.rebuilt)


_SMALL_SIZE = [serial_size(st) if st not in (10, 11) else 0 for st in range(128)]


def _types(buf, pos, stop, max_cols=MAX_COLUMNS, limit=None):
    """Serial types between pos and stop (at most max_cols): (types, body_size) or None.
    limit: the body size the cell allows - a header asking for more is no cell (stops
    early; the carver tries millions of positions). One-byte types, nearly all of them, are
    read without the general varint reader."""
    types = []
    append = types.append
    body = 0
    small = _SMALL_SIZE
    while pos < stop:
        if len(types) >= max_cols:
            return None
        b = buf[pos]
        if b < 0x80:
            st = b
            pos += 1
            if st == 10 or st == 11:
                return None
            body += small[st]
        else:
            r = varint(buf, pos, stop)
            if r is None:
                return None
            st, pos = r
            body += serial_size(st)
        append(st)
        if limit is not None and body > limit:
            return None
    return (types, body) if pos == stop else None


def _count_types(buf, pos, count, end):
    """Exactly `count` serial types from pos: (types, next_pos, body_size) or None."""
    types, body = [], 0
    for _ in range(count):
        r = varint(buf, pos, end)
        if r is None:
            return None
        st, pos = r
        if st == 10 or st == 11:
            return None
        types.append(st)
        body += serial_size(st)
    return types, pos, body


def parse_intact(buf, pos, end, usable, index, max_cols=MAX_COLUMNS, cut=False):
    """The complete cell starting at buf[pos] and ending at or before `end`, or None.
    Records with more than max_cols columns are not looked for.

    cut: also a cell whose header is whole but whose (all local) body runs past `end` - a
    newer cell overwrote its tail; it is returned with local = the bytes before `end`."""
    r = varint(buf, pos, end)
    if r is None or r[0] < 1:
        return None
    plen, p = r
    rowid = None
    if not index:
        r = varint(buf, p, end)
        if r is None:
            return None
        rowid, p = to_signed64(r[0]), r[1]
    r = varint(buf, p, end)
    if r is None:
        return None
    hlen, q = r
    if hlen < 2 or hlen > plen or hlen > 9 * max_cols + 2:
        return None
    hend = p + hlen
    if hend > end:
        return None
    parsed = _types(buf, q, hend, max_cols, plen - hlen)
    if parsed is None:
        return None
    types, body = parsed
    if plen != hlen + body:
        return None
    local = local_payload_size(plen, usable, not index)
    if local < hlen:
        return None
    ovfl = None
    cend = p + local
    if local < plen:
        if cend + 4 > end:
            return None
        ovfl = _U32.unpack_from(buf, cend)[0]
        if ovfl == 0:
            return None
        cend += 4
    if cend > end:
        if cut and ovfl is None and hend < end:
            return Cell(pos, index, plen, rowid, p, hlen, types, hend, end - p, end, None)
        return None
    return Cell(pos, index, plen, rowid, p, hlen, types, hend, local, cend, ovfl)


def stale_header(buf, pos, end, usable):
    """Size recorded by a freeblock header SQLite may have written at buf[pos], or None.

    The header may be stale: the free run it describes can have been merged, shrunk (space
    is allocated from a freeblock's end) or partly reused since, so its size is only required
    to stay inside the page (not inside `end`); the next-pointer must be 0 or point past it.
    """
    if pos + 4 > end:
        return None
    nxt, size = _U16.unpack_from(buf, pos)[0], _U16.unpack_from(buf, pos + 2)[0]
    if size < 4 or pos + size > usable:
        return None
    if nxt and not (pos + size <= nxt <= usable - 4):
        return None
    return size


def _shape_ok(buf, start, length, intact_from):
    """Could the bytes buf[start:start+length] that survive (index >= intact_from) be part of
    one minimal varint of that length?"""
    last = start + length - 1
    if last >= len(buf):
        return False                    # past the page's end (a free area at its very end)
    for p in range(max(start, intact_from), start + length):
        b = buf[p]
        if p == last:
            if length < 9 and b & 0x80:
                return False
        elif not b & 0x80:
            return False
        if p == start and length > 1 and b == 0x80:
            return False
    return True


_SPLITS = {0: [()], 1: [(1,)], 2: [(1, 1), (2,)], 3: [(1, 1, 1), (1, 2), (2, 1), (3,)],
           4: [(1, 1, 1, 1), (1, 1, 2), (1, 2, 1), (2, 1, 1), (2, 2), (1, 3), (3, 1), (4,)]}
_VAR_RANGE = {1: (0, 57), 2: (58, 8185), 3: (8186, 1048569), 4: (1048570, 134217721)}


def _var_type(kind, size, vlen):
    lo, hi = _VAR_RANGE[vlen]
    if not lo <= size <= hi:
        return None
    return 2 * size + (13 if kind == "text" else 12)


def _layouts(buf, pos, usable, index, clobber):
    """Varint lengths (payload length, rowid, header length) the surviving bytes allow for a
    cell at pos: [(lp, lr, lh, header_pos, types_pos, first_intact_type_pos, header_len or
    None, rowid or None)]."""
    intact = pos + clobber
    out = []
    for lr in ((0,) if index else range(1, 9)):
        for lp in (1, 2, 3):
            if lr and not _shape_ok(buf, pos + lp, lr, intact):
                continue
            a = pos + lp + lr                        # header-length varint
            rowid = None
            if lr and pos + lp >= intact:
                r = varint(buf, pos + lp, a)
                if r is None or r[1] != a:
                    continue
                rowid = to_signed64(r[0])
            for lh in (1, 2):
                if not _shape_ok(buf, a, lh, intact):
                    continue
                t0 = a + lh
                if t0 >= usable:
                    continue
                known_h = None
                if a >= intact:
                    r = varint(buf, a, usable)
                    if r is None or r[1] != t0:
                        continue
                    known_h = r[0]
                out.append((lp, lr, lh, a, t0, max(t0, intact), known_h, rowid))
    return out


def self_ends(buf, pos, end, usable, index, counts, clobber=4):
    """Where a cell at pos ends by its own header, when the overwritten bytes held only the
    lengths (payload, rowid, header) and every column type survived: the types count `n`
    (one of `counts`) from the first intact byte, the body follows them, and the payload
    length they give fits the varint and the page. Ends between pos and `end` only, sorted.
    For a freed cell whose neighbour lost its own start (no boundary to end it at)."""
    out = set()
    for lp, lr, lh, a, t0, first, known_h, _rowid in _layouts(buf, pos, usable, index,
                                                              clobber):
        if first != t0:
            continue                    # a column type was overwritten: no own length
        for n in counts:
            got = _count_types(buf, t0, n, end)
            if not got:
                continue
            _types, hend, body = got
            if known_h is not None and known_h != hend - a:
                continue
            if varint_len(hend - a) != lh:
                continue
            plen = hend - a + body
            e = hend + body
            if pos + clobber < e < end and varint_len(plen) == lp and \
                    local_payload_size(plen, usable, not index) == plen:
                out.add(e)
    return sorted(out)


def reconstruct(buf, pos, end, usable, index, tpl, clobber=4, cache=None):
    """Every way the cell occupying exactly buf[pos:end] can be a record of template `tpl`
    when its first `clobber` bytes were overwritten. Returns a list of Cells (rowid None
    unless its bytes survived). `cache` (a dict) may be shared by calls for the same pos,
    index and clobber: what does not depend on the end or the template is worked out once."""
    return [cell for _t, cell in reconstruct_all(buf, pos, end, usable, index, [tpl], clobber,
                                                 cache)]


def reconstruct_all(buf, pos, end, usable, index, tpls, clobber=4, cache=None,
                    partial=False):
    """reconstruct() for several templates at once: [(template, Cell)]. The byte layout
    work is shared by every template with the same column count.

    partial: also the cells whose body runs past `end` (an update to a shorter value wrote
    its new cell over the old one's tail): the lost leading columns are taken as storing no
    bytes (a rowid alias, NULLs) and the payload as cut at `end`; the caller decides
    whether what survives is worth keeping."""
    size = end - pos
    out = []
    if size < 4:
        return out
    by_n = {}
    for t in tpls:
        if t.n >= 1:
            by_n.setdefault(t.n, []).append(t)
    if not by_n:
        return out
    # which templates a cache entry was worked out for (one cache serves several groups)
    gid = dict((n, tuple(id(t) for t in g)) for n, g in by_n.items())
    if cache is None:
        cache = {}
    layouts = cache.get("layouts")
    if layouts is None:
        layouts = cache["layouts"] = _layouts(buf, pos, usable, index, clobber)
    min_local = ((usable - 12) * 32 // 255) - 23
    for lp, lr, lh, a, t0, first, known_h, rowid in layouts:
        if lp + lr >= size or t0 >= end:
            continue
        plen = size - lp - lr                        # payload when nothing overflows
        whole = varint_len(plen) == lp and local_payload_size(plen, usable, not index) == plen
        spills = plen - 4 >= min_local               # or: local part + 4-byte overflow pointer
        if not (whole or spills):
            continue
        for lens in _SPLITS.get(first - t0, ()):
            j = len(lens)
            for n, group in by_n.items():
                if j > n:
                    continue
                key = (first, n - j)
                tail = cache.get(key)
                if tail is None:
                    tail = cache[key] = _count_types(buf, first, n - j, usable) or ()
                if not tail:
                    continue
                types_tail, hend, known_body = tail
                if hend > end:
                    continue
                hlen = hend - a
                if varint_len(hlen) != lh or (known_h is not None and known_h != hlen):
                    continue
                rest = plen - hlen - known_body      # bytes of the lost columns
                fkey = ("fits", key, gid[n])
                fits = cache.get(fkey)
                if fits is None:                     # the templates whose columns j.. take
                    fits = cache[fkey] = [           # these types (the same for every end)
                        tpl for tpl in group
                        if all(tpl.type_ok(j + i, st)[0] for i, st in enumerate(types_tail))]
                for tpl in fits:
                    if spills:
                        for cell in _with_overflow(buf, pos, end, usable, index, tpl, lp, lr, a,
                                                   hlen, lens, types_tail, hend, known_body,
                                                   rowid, clobber):
                            out.append((tpl, cell))
                    if partial and rest < 0 and not spills:
                        for head, solved in _assign(tpl, lens, 0):
                            out.append((tpl, Cell(pos, index, hlen + known_body, rowid, a,
                                                  hlen, head + types_tail, hend, plen, end,
                                                  None, clobber, solved, j)))
                        continue
                    if not whole or rest < 0:
                        continue
                    for head, solved in _assign(tpl, lens, rest):
                        out.append((tpl, Cell(pos, index, plen, rowid, a, hlen, head + types_tail,
                                              hend, plen, end, None, clobber, solved, j)))
    return out


MAX_ASSIGNMENTS = 64


def _assign(tpl, lens, rest):
    """Serial types for the lost leading columns whose sizes add up to `rest`:
    [(types, solved_positions)] - remembered per template (the answer depends only on the
    template, lens and rest; a carve asks the same question millions of times). The memo is
    dropped when the template learns lengths (Template.forget_assignments).

    Only the last lost column can take whatever size is left. When an earlier lost column is
    text or blob too, its size cannot be read off anything, so only the lengths seen in the
    table's live rows (Template.length_prior) are tried; without any, no split is guessed.
    A template with `free_variable` set (index entries: a short text key followed by the
    rowid) also tries every size that leaves room for some combination of the fixed sizes of
    the columns after it, when those are all fixed-size."""
    memo = getattr(tpl, "_assign_memo", None)
    if memo is None:
        memo = {}
        try:
            tpl._assign_memo = memo
        except AttributeError:          # a template that keeps no memo: computed each time
            pass
    key = (tuple(lens), rest)
    got = memo.get(key)
    if got is None:
        got = memo[key] = _assign_now(tpl, lens, rest)
    return got


def _assign_now(tpl, lens, rest):
    options = [tpl.size_options(i, vlen) for i, vlen in enumerate(lens)]
    tails = [None] * (len(lens) + 1)          # possible fixed-size sums of columns i..end
    if getattr(tpl, "free_variable", False):
        tails[len(lens)] = set([0])
        for i in range(len(lens) - 1, -1, -1):
            sizes = [f for f, _st in options[i]]
            if tails[i + 1] is None or None in sizes:
                break
            tails[i] = set(a + b for a in set(sizes) for b in tails[i + 1])
    # smallest / largest total size the columns from position i on can still take
    lo, hi = [0] * (len(lens) + 1), [0] * (len(lens) + 1)
    for i in range(len(lens) - 1, -1, -1):
        sizes = [f for f, _st in options[i] if f is not None]
        var = [_VAR_RANGE[lens[i]] for f, _st in options[i] if f is None]
        mins = sizes + [r[0] for r in var]
        maxs = sizes + [r[1] for r in var]
        if not mins:
            return []
        lo[i], hi[i] = lo[i + 1] + min(mins), hi[i + 1] + max(maxs)
    results = []
    budget = [MAX_ASSIGNMENTS * 64]

    def walk(i, left, acc, solved):
        budget[0] -= 1
        if len(results) >= MAX_ASSIGNMENTS or budget[0] < 0 or not lo[i] <= left <= hi[i]:
            return
        if i == len(lens):
            if left == 0:
                results.append((list(acc), tuple(solved)))
            return
        last = i == len(lens) - 1
        for fsize, st in options[i]:
            if fsize is None:                       # text / blob of unknown size
                if last:
                    t = _var_type(st, left, lens[i])
                    if t is not None:
                        walk(i + 1, 0, acc + [t], solved + [i])
                else:
                    sizes = set(tpl.length_prior[i])
                    if tails[i + 1] is not None:
                        sizes.update(left - x for x in tails[i + 1] if x <= left)
                    for s in sorted(sizes):
                        t = _var_type(st, s, lens[i])
                        if t is not None and s <= left:
                            walk(i + 1, left - s, acc + [t], solved + [i])
            elif fsize <= left:
                walk(i + 1, left - fsize, acc + [st], solved)
    walk(0, rest, [], [])
    return results


def _with_overflow(buf, pos, end, usable, index, tpl, lp, lr, a, hlen, lens, types_tail, hend,
                   known_body, rowid, clobber):
    """Lost leading types for a cell that spills to overflow pages: only fixed-size types can
    be solved (the payload length is lost with them)."""
    out = []
    checked = {}                        # total size of the lost columns -> (local, ovfl) or None
    for size, head in _fixed_combos(tpl, lens):
        got = checked.get(size, False)
        if got is False:
            got = None
            plen = hlen + known_body + size
            if varint_len(plen) == lp:
                local = local_payload_size(plen, usable, not index)
                if local < plen and a + local + 4 == end:
                    ovfl = _U32.unpack_from(buf, a + local)[0]
                    if ovfl:
                        got = (plen, local, ovfl)
            checked[size] = got
        if got is not None:
            plen, local, ovfl = got
            out.append(Cell(pos, index, plen, rowid, a, hlen, head + types_tail, hend, local,
                            end, ovfl, clobber, (), len(lens)))
    return out


def _fixed_combos(tpl, lens):
    """[(total size, serial types)] of every fixed-size choice for the lost leading columns,
    in the order they are tried - they depend only on the template and the lost varints'
    lengths, so they are worked out once per template (a large carve asks millions of times)."""
    memo = getattr(tpl, "_ovfl_memo", None)
    if memo is None:
        memo = {}
        try:
            tpl._ovfl_memo = memo
        except AttributeError:
            pass
    key = tuple(lens)
    got = memo.get(key)
    if got is None:
        options = [[o for o in tpl.size_options(i, vlen) if o[0] is not None]
                   for i, vlen in enumerate(lens)]
        got = []

        def walk(i, acc, size):
            if i == len(lens):
                got.append((size, acc))
                return
            for fsize, st in options[i]:
                walk(i + 1, acc + [st], size + fsize)
        walk(0, [], 0)
        memo[key] = got
    return got
