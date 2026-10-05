"""Write-ahead log reader: frames, checksums, commit grouping, frame states.

Header and frame-header fields are ALWAYS big-endian. The magic number only
selects the byte order of the 32-bit words fed to the checksum
(0x377f0682 = little-endian words, 0x377f0683 = big-endian words).
Replay follows SQLite's own recovery: frames are valid while their salts match
the header and the running checksum verifies; everything up to the last valid
commit frame is committed.

The frame count comes from the file size, never from a header field. Each frame costs a few
bytes of compact arrays (WalFrame objects are made when asked for), the parse can be stopped
(cancel) and is limited to a number of frames (limits 'wal_frames', set by the caller).
"""

import mmap
import os
import struct
from array import array

WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24

CURRENT = "current"          # committed, latest committed version of its page
SUPERSEDED = "superseded"    # committed, but a later committed frame has the same page
UNCOMMITTED = "uncommitted"  # current salts, after the last valid commit (in-progress or rolled
                             # back). SQLite re-checksums overwritten frames only at commit, so
                             # these may legitimately fail the checksum; see WalFrame.checksum_ok.
STALE = "stale"              # salt mismatch: left over from an earlier WAL generation
STATES = (CURRENT, SUPERSEDED, UNCOMMITTED, STALE)


class WalError(ValueError):
    pass


class WalCancelled(Exception):
    """Reading the WAL was stopped by the caller."""


def wal_checksum(data, s0, s1, big_endian_words):
    """SQLite WAL checksum over len(data) (multiple of 8) bytes, seeded with (s0, s1)."""
    n = len(data) // 4
    words = struct.unpack_from((">%dI" if big_endian_words else "<%dI") % n, data)
    mask = 0xFFFFFFFF
    for i in range(0, n, 2):
        s0 = (s0 + words[i] + s1) & mask
        s1 = (s1 + words[i + 1] + s0) & mask
    return s0, s1


class WalHeader(object):
    __slots__ = ("magic", "version", "page_size", "checkpoint_seq", "salt1", "salt2",
                 "checksum1", "checksum2", "big_endian_words", "checksum_ok")


class WalFrame(object):
    """One frame, made on demand from the compact per-frame arrays of a WalFile."""
    __slots__ = ("index", "offset", "page_no", "db_size", "salt1", "salt2",
                 "checksum1", "checksum2", "salt_match", "checksum_ok", "state",
                 "commit_group", "page_type")

    @property
    def is_commit(self):
        return self.db_size > 0

    def __eq__(self, other):
        return isinstance(other, WalFrame) and self.offset == other.offset

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.offset)


_OK = {None: 0, True: 1, False: 2}
_OK_BACK = (None, True, False)
_STATE_NO = dict((s, i) for i, s in enumerate(STATES))


class _Frames(object):
    """The frames of a WalFile as a read-only sequence of WalFrame objects, each made when it
    is asked for."""

    def __init__(self, wal):
        self._w = wal

    def __len__(self):
        return len(self._w._page_no)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self._make(j) for j in range(*i.indices(len(self)))]
        n = len(self)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("frame index out of range")
        return self._make(i)

    def __iter__(self):
        for i in range(len(self)):
            yield self._make(i)

    def __bool__(self):
        return len(self) > 0

    __nonzero__ = __bool__

    def _make(self, i):
        w = self._w
        fr = WalFrame()
        fr.index, fr.offset = i, WAL_HEADER_SIZE + i * w._frame_size
        fr.page_no, fr.db_size = w._page_no[i], w._db_size[i]
        fr.salt1, fr.salt2 = w._salt1[i], w._salt2[i]
        fr.checksum1, fr.checksum2 = w._c1[i], w._c2[i]
        fr.salt_match = bool(w._flags[i] & 1)
        fr.checksum_ok = _OK_BACK[w._flags[i] >> 1]
        fr.state = STATES[w._state[i]]
        g = w._group[i]
        fr.commit_group = None if g < 0 else g
        fr.page_type = w._ptype[i]
        return fr


class WalFile(object):
    """Read-only view of a -wal file. Never writes; uses an ACCESS_READ mmap.

    cancel() -> True stops the parse (WalCancelled). At most max_frames frames are read:
    frames_cut is then True and frames_total says how many the file holds. progress(text) is
    told how far the parse is on a large WAL.
    """

    def __init__(self, path, cancel=None, max_frames=None, progress=None):
        self.path = path
        self.size = os.path.getsize(path)
        if self.size < WAL_HEADER_SIZE:
            raise WalError("WAL file shorter than its 32-byte header")
        self._f = open(path, "rb")
        try:
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._f.close()
            raise
        try:
            self.header = self._parse_header()
            self.page_size = self.header.page_size
            self._frame_size = FRAME_HEADER_SIZE + self.page_size
            self.frames_total = max(0, (self.size - WAL_HEADER_SIZE) // self._frame_size)
            self.frames_cut = max_frames is not None and self.frames_total > max_frames
            self._page_no, self._db_size = array("I"), array("I")
            self._salt1, self._salt2 = array("I"), array("I")
            self._c1, self._c2 = array("I"), array("I")
            self._flags, self._ptype = array("B"), array("B")
            self._state, self._group = array("B"), array("l")
            self.frames = _Frames(self)
            self.last_commit = -1
            self._parse_frames(cancel, max_frames if self.frames_cut else self.frames_total,
                               progress)
            self.overlay = {}
            self.db_size_pages = None
            self._classify()
        except BaseException:
            self.close()
            raise

    def close(self):
        mm, self._mm = getattr(self, "_mm", None), None
        if mm is not None:
            mm.close()
        f, self._f = getattr(self, "_f", None), None
        if f is not None:
            f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _parse_header(self):
        mm = self._mm
        (magic, version, page_size, ckpt, salt1, salt2,
         c1, c2) = struct.unpack_from(">8I", mm, 0)
        if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
            raise WalError("bad WAL magic 0x%08x" % magic)
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise WalError("bad WAL page size %d" % page_size)
        h = WalHeader()
        h.magic, h.version, h.page_size, h.checkpoint_seq = magic, version, page_size, ckpt
        h.salt1, h.salt2, h.checksum1, h.checksum2 = salt1, salt2, c1, c2
        h.big_endian_words = magic == WAL_MAGIC_BE
        h.checksum_ok = wal_checksum(bytes(mm[0:24]), 0, 0, h.big_endian_words) == (c1, c2)
        return h

    def _parse_frames(self, cancel, count, progress):
        mm, h = self._mm, self.header
        frame_size, page_size = self._frame_size, h.page_size
        s0, s1 = h.checksum1, h.checksum2
        chain_ok = h.checksum_ok
        big = h.big_endian_words
        hs1, hs2 = h.salt1, h.salt2
        unpack = struct.unpack_from
        add_page, add_db = self._page_no.append, self._db_size.append
        add_s1, add_s2 = self._salt1.append, self._salt2.append
        add_c1, add_c2 = self._c1.append, self._c2.append
        add_flags, add_type = self._flags.append, self._ptype.append
        offset = WAL_HEADER_SIZE
        for index in range(count):
            if index and not index & 0x3FFF:
                if cancel is not None and cancel():
                    raise WalCancelled("stopped at WAL frame %d" % index)
                if progress is not None and not index & 0xFFFF:
                    progress("reading the WAL: frame %s of %s"
                             % (format(index, ","), format(count, ",")))
            page_no, db_size, salt1, salt2, c1, c2 = unpack(">6I", mm, offset)
            salt_match = salt1 == hs1 and salt2 == hs2
            ok = None                       # None = not verifiable (chain already broken)
            data_off = offset + FRAME_HEADER_SIZE
            type_off = data_off + (100 if page_no == 1 else 0)
            if chain_ok and salt_match and page_no > 0:
                s0, s1 = wal_checksum(mm[offset:offset + 8], s0, s1, big)
                s0, s1 = wal_checksum(mm[data_off:data_off + page_size], s0, s1, big)
                ok = (s0, s1) == (c1, c2)
                if ok:
                    if db_size > 0:
                        self.last_commit = index
                else:
                    chain_ok = False
            else:
                chain_ok = False
            add_page(page_no)
            add_db(db_size)
            add_s1(salt1)
            add_s2(salt2)
            add_c1(c1)
            add_c2(c2)
            add_flags((1 if salt_match else 0) | (_OK[ok] << 1))
            add_type(mm[type_off] if type_off < self.size else 0)
            offset += frame_size
        n = len(self._page_no)
        self._state = array("B", [_STATE_NO[STALE]]) * n
        self._group = array("l", [-1]) * n

    def _classify(self):
        group = 0
        latest = {}
        page_no, db_size, flags = self._page_no, self._db_size, self._flags
        state, groups = self._state, self._group
        uncommitted = _STATE_NO[UNCOMMITTED]
        for i in range(len(page_no)):
            if i <= self.last_commit:
                groups[i] = group
                latest[page_no[i]] = i
                if db_size[i] > 0:
                    group += 1
            elif flags[i] & 1:
                state[i] = uncommitted
        cur, sup = _STATE_NO[CURRENT], _STATE_NO[SUPERSEDED]
        for i in range(self.last_commit + 1):
            state[i] = cur if latest[page_no[i]] == i else sup
        self.overlay = latest
        if self.last_commit >= 0:
            self.db_size_pages = db_size[self.last_commit]

    @property
    def commit_count(self):
        d = self._db_size
        return sum(1 for i in range(self.last_commit + 1) if d[i] > 0)

    def page_data(self, frame_index):
        """Raw page bytes stored in the given frame."""
        n = len(self._page_no)
        if frame_index < 0:
            frame_index += n
        if not 0 <= frame_index < n:
            raise IndexError("frame index out of range")
        start = WAL_HEADER_SIZE + frame_index * self._frame_size + FRAME_HEADER_SIZE
        return bytes(self._mm[start:start + self.page_size])

    def state_counts(self):
        return dict((s, self._state.count(i)) for i, s in enumerate(STATES))
