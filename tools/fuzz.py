"""Mutation fuzzer for everything the tool parses from untrusted files (standard library only).

Every evidence byte is attacker-controlled: database pages, -wal and -journal files, BLOB
values. This fuzzer mutates small seed inputs it builds itself (databases made with sqlite3,
compressed streams, plists, JSON, XML, protobuf, images...) and feeds them to the parsers and
decoders, looking for:

  - an exception a public API should never raise (each target lists what it may raise);
  - a slow input (more than --slow seconds for one input; --slow-db for whole databases);
  - a memory blow-up (process peak above --mem MB), and a hang or crash of the child.

usage:
  python tools/fuzz.py [--minutes M | --seconds S] [--out DIR] [--seed N] [--mem MB]
                       [target ...]
  python tools/fuzz.py --list

Without targets every target is run, the time shared between them (a database target gets
four shares: its inputs are slower). Each target runs in a
child process (restarted after a memory exit, a crash or a hang), whose committed memory is
capped on Windows (a job object) and on POSIX (RLIMIT_AS) at twice --mem, so a bomb ends in
MemoryError instead of exhausting the machine. Findings (the input and a .txt with the reason)
go to --out (default: a folder in the system temp directory); nothing is written anywhere else.
The exit status is 0 when nothing was found, 1 otherwise.

tests/test_fuzz_quick.py runs run_inputs() in-process for a few seconds with a fixed seed.
"""

import argparse
import bz2
import gzip
import hashlib
import json
import lzma
import os
import plistlib
import random
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

BLOB_TARGETS = ("decode_blob", "summary", "interpretations", "decoded_strings", "lz:lz4",
                "lz:lz4_apple", "lz:lzfse", "lz:lzvn", "zstd", "compress:gzip",
                "compress:zlib", "compress:bz2", "compress:xz", "compress:lzma", "record",
                "record_strict", "pretty_xml", "pretty_json", "schema_text")
DB_TARGETS = ("db_native", "db_session", "db_safe")
TARGETS = BLOB_TARGETS + DB_TARGETS

INTERESTING = [0, 1, 0x7F, 0x80, 0xFF, 0x7FFF, 0x8000, 0xFFFF, 0x7FFFFFFF, 0x80000000,
               0xFFFFFFFF, 0x10000, 0x40000000, 1 << 31, 1 << 63, (1 << 64) - 1]


# -- seeds ----------------------------------------------------------------------------------
def _lz4_frame(payload):
    """An LZ4 frame holding payload in stored (uncompressed) blocks, with a content checksum."""
    from engine.decode.lz import xxh32
    flg = 0x64 | 0x04                       # version 1, independent blocks, content checksum
    bd = 0x40                               # 64 KiB blocks
    desc = bytes([flg, bd])
    out = bytearray(b"\x04\x22\x4d\x18") + desc + bytes([(xxh32(desc) >> 8) & 0xFF])
    for i in range(0, len(payload), 65536):
        chunk = payload[i:i + 65536]
        out += struct.pack("<I", len(chunk) | 0x80000000) + chunk
    out += b"\x00\x00\x00\x00" + struct.pack("<I", xxh32(payload))
    return bytes(out)


def _zstd_raw(payload):
    """A zstd frame of raw blocks with a content size and checksum."""
    from engine.decode.zstd import xxh64
    fhd = 0x20 | 0x04 | 0x00                # single segment, checksum, 1-byte content size
    if len(payload) > 255:
        fhd = 0x20 | 0x04 | 0x40            # 2-byte content size (value - 256)
        size = struct.pack("<H", len(payload) - 256)
    else:
        size = bytes([len(payload)])
    out = bytearray(struct.pack("<I", 0xFD2FB528)) + bytes([fhd]) + size
    blocks = [payload[i:i + 1000] for i in range(0, len(payload), 1000)] or [b""]
    for k, chunk in enumerate(blocks):
        last = 1 if k == len(blocks) - 1 else 0
        hdr = (len(chunk) << 3) | last       # type 0 = raw
        out += struct.pack("<I", hdr)[:3] + chunk
    out += struct.pack("<I", xxh64(payload) & 0xFFFFFFFF)
    return bytes(out)


def _apple_lz4(payload):
    return b"bv4-" + struct.pack("<I", len(payload)) + payload + b"bv4$"


def _lzfse_stored(payload):
    return b"bvx-" + struct.pack("<I", len(payload)) + payload + b"bvx$"


def _protobuf():
    def varint(v):
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            out.append(b | (0x80 if v else 0))
            if not v:
                return bytes(out)
    inner = b"\x08" + varint(150) + b"\x12\x05hello"
    return b"\x0a" + varint(len(inner)) + inner + b"\x10" + varint(1 << 40) + b"\x1d" + \
        struct.pack("<f", 1.5) + b"\x22\x03\x01\x02\x03"


def _png(w=4, h=4):
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def blob_seeds():
    text = (b"The quick brown fox jumps over the lazy dog. " * 40)
    plist = {"name": "Alice", "n": 42, "when": 700000000.5, "data": b"\x00\x01" * 20,
             "list": [1, 2.5, "three", {"k": [True, False]}]}
    seeds = [
        gzip.compress(text), zlib.compress(text), zlib.compress(zlib.compress(text)),
        bz2.compress(text), lzma.compress(text),
        lzma.compress(text, format=lzma.FORMAT_ALONE), _lz4_frame(text), _apple_lz4(text),
        _lzfse_stored(text), _zstd_raw(text), plistlib.dumps(plist, fmt=plistlib.FMT_BINARY),
        plistlib.dumps(plist), json.dumps(plist, default=str).encode(),
        b"<?xml version='1.0'?><r a='1'><b>t &amp; u</b><c/><!-- c --><![CDATA[x<y]]></r>",
        _protobuf(), _png(), b"\xff\xd8\xff\xe0" + bytes(64), b"GIF89a" + bytes(20),
        bytes(range(256)), b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 3,
        struct.pack("<q", 132000000000000000), b"\x04\x01\x0f\x00\x07abc",
        b"\x05\x18\x81\x01\x07" + bytes(80), text.decode().encode("utf-16-le"),
    ]
    return [s for s in seeds if s]


SCHEMA_SEEDS = [
    "CREATE TABLE t(a INTEGER PRIMARY KEY, b TEXT NOT NULL DEFAULT 'x', c REAL, d BLOB)",
    "CREATE TABLE w(k TEXT, v, PRIMARY KEY(k DESC)) WITHOUT ROWID",
    "CREATE TABLE g(a, b GENERATED ALWAYS AS (a * 2) VIRTUAL, c AS (upper(a)) STORED)",
    "CREATE TABLE \"we\"\"ird\"([x y] TEXT COLLATE NOCASE CHECK(length([x y]) < 9), "
    "`z` INT REFERENCES t(a) ON DELETE CASCADE)",
    "CREATE VIRTUAL TABLE f USING fts5(body, title)",
    "CREATE INDEX i ON t(b COLLATE NOCASE, c DESC) WHERE c > 0",
    "CREATE TABLE x AS SELECT 1",
    "CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; SELECT 2; END",
]


def make_db_seeds(folder):
    """Small databases (with -wal / -journal sidecars) built with sqlite3 in folder."""
    out = []

    def build(name, statements, page_size=1024, mode="DELETE", keep_wal=False, journal=False):
        path = os.path.join(folder, name)
        c = sqlite3.connect(path)
        c.execute("PRAGMA page_size=%d" % page_size)
        c.execute("PRAGMA journal_mode=%s" % mode)
        for st in statements:
            c.execute(st)
        c.commit()
        sides = {}
        if keep_wal:
            # copy the -wal while the connection still holds it (a later close checkpoints)
            c.execute("PRAGMA wal_autocheckpoint=0")
            c.execute("INSERT INTO t(b) VALUES ('after wal')")
            c.commit()
            with open(path + "-wal", "rb") as f:
                sides["-wal"] = f.read()
            with open(path, "rb") as f:
                main = f.read()
        if journal:
            c.execute("PRAGMA journal_mode=DELETE")
            c.execute("BEGIN")
            c.execute("UPDATE t SET b = 'changed'")
            with open(path, "rb") as f:
                main = f.read()
            if os.path.exists(path + "-journal"):
                with open(path + "-journal", "rb") as f:
                    sides["-journal"] = f.read()
            c.rollback()
        c.close()
        if not keep_wal and not journal:
            with open(path, "rb") as f:
                main = f.read()
        for side in ("-wal", "-shm", "-journal"):
            if os.path.exists(path + side):
                os.remove(path + side)
        os.remove(path)
        out.append((name, main, sides))

    rows = ["INSERT INTO t(b, c, d) VALUES ('%s', %d.5, zeroblob(%d))" % ("v" * (i % 50), i,
                                                                          (i * 37) % 3000)
            for i in range(60)]
    build("plain.db", [SCHEMA_SEEDS[0]] + rows + ["CREATE INDEX i ON t(b)",
                                                  "CREATE VIEW v AS SELECT a, b FROM t",
                                                  "DELETE FROM t WHERE a % 3 = 0"])
    build("norowid.db", [SCHEMA_SEEDS[1]] + ["INSERT INTO w VALUES ('k%d', %d)" % (i, i)
                                             for i in range(200)])
    build("gen.db", [SCHEMA_SEEDS[2], "INSERT INTO g(a) VALUES (1), ('x'), (NULL)"])
    build("vacuum.db", ["PRAGMA auto_vacuum=FULL", SCHEMA_SEEDS[0]] + rows, page_size=512)
    build("wal.db", [SCHEMA_SEEDS[0]] + rows[:20], mode="WAL", keep_wal=True)
    build("journal.db", [SCHEMA_SEEDS[0]] + rows[:30], journal=True)
    build("utf16.db", ["PRAGMA encoding='UTF-16le'", SCHEMA_SEEDS[0]] + rows[:10])
    return out


# -- mutation -------------------------------------------------------------------------------
def mutate(rng, data, others=()):
    b = bytearray(data)
    for _ in range(rng.choice((1, 1, 2, 3, 5, 8))):
        n = len(b)
        if n == 0:
            b += bytes(rng.getrandbits(8) for _ in range(8))
            continue
        i = rng.randrange(n)
        op = rng.randrange(9)
        if op == 0:
            b[i] ^= 1 << rng.randrange(8)
        elif op == 1:
            b[i] = rng.choice((0, 0xFF, 0x7F, 0x80, 0x81, 0x01))
        elif op == 2:
            b[i:i] = bytes(rng.getrandbits(8) for _ in range(rng.choice((1, 2, 4, 16, 64))))
        elif op == 3:
            del b[i:i + rng.choice((1, 2, 4, 16, 64))]
        elif op == 4:
            w = rng.choice((2, 4, 8))
            v = rng.choice(INTERESTING + [rng.getrandbits(32)]) & ((1 << (8 * w)) - 1)
            b[i:i + w] = v.to_bytes(w, rng.choice(("big", "little")))
        elif op == 5:
            k = rng.choice((2, 5, 9, 10, 20))
            b[i:i + k] = b"\xff" * (k - 1) + b"\x7f"
        elif op == 6:
            j = rng.randrange(n)
            b[i:i] = b[j:j + rng.choice((4, 16, 64, 256))]
        elif op == 7 and n >= 8:
            w = rng.choice((2, 4))
            j = rng.randrange(max(1, n - w))
            b[i:i + w] = b[j:j + w]
        elif op == 8 and others:
            o = rng.choice(others)
            j = rng.randrange(len(o) or 1)
            b[i:] = o[j:j + rng.randrange(1, 512)]
    if rng.random() < 0.03 and b:
        b = b * rng.choice((2, 16, 256))
    return bytes(b[:1 << 20])


def mutate_db(rng, data, page_size):
    """Structure-aware: page headers, cell pointers, page pointers, header fields, varints."""
    b = bytearray(data)
    pages = max(1, len(b) // page_size)
    for _ in range(rng.choice((1, 2, 3, 6))):
        pg = rng.randrange(pages)
        base = pg * page_size + (100 if pg == 0 else 0)
        what = rng.randrange(7)
        if what == 0:
            off = base + rng.choice((0, 1, 3, 5, 7, 8))
            w = 1 if off - base in (0, 7) else (4 if off - base == 8 else 2)
            v = rng.choice((0, 1, 0xFF, 0xFFFF, 0x7FFF, pg + 1, rng.randrange(1, pages + 3)))
            b[off:off + w] = (v & ((1 << (8 * w)) - 1)).to_bytes(w, "big")
        elif what == 1:
            off = base + 8 + 2 * rng.randrange(8)
            b[off:off + 2] = rng.choice((0, page_size - 1, rng.randrange(page_size))).to_bytes(
                2, "big")
        elif what == 2:
            off = pg * page_size + rng.randrange(max(1, page_size - 4))
            b[off:off + 4] = rng.choice((pg + 1, 1, 2, 0xFFFFFFFF, pages + 5)).to_bytes(4, "big")
        elif what == 3:
            off = rng.choice((28, 32, 36, 52, 56, 64, 92, 18, 19, 20, 16))
            w = 1 if off in (18, 19, 20) else (2 if off == 16 else 4)
            v = rng.choice((0, 1, 2, 3, 0xFFFFFFFF, pages + 1, 0x7FFFFFFF, 65535))
            b[off:off + w] = (v & ((1 << (8 * w)) - 1)).to_bytes(w, "big")
            if off == 28 and rng.random() < 0.7:
                b[92:96] = b[24:28]             # make the declared page count trusted
        elif what == 4:
            off = pg * page_size + rng.randrange(max(1, page_size - 9))
            b[off:off + 9] = rng.choice((b"\xff" * 8 + b"\x7f", b"\x88\x80\x80\x80\x00",
                                         b"\x84\x80\x80\x80\x00", b"\x87\xff\xff\xff\x7f"))
        elif what == 5:
            # schema text: overwrite part of sqlite_master's SQL with a hostile statement
            evil = rng.choice((b"CREATE TABLE x AS WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL "
                               b"SELECT i+1 FROM c) SELECT i FROM c",
                               b"CREATE TABLE x(a); ATTACH 'x' AS y; SELECT 1",
                               b"CREATE TABLE x(a)\x00junk", b"\xff\xfe\xfd",
                               b"CREATE VIEW v AS SELECT zeroblob(1000000000)"))
            k = bytes(b).find(b"CREATE ")
            if k >= 0:
                b[k:k + len(evil)] = evil
        else:
            b = bytearray(mutate(rng, bytes(b)))
    return bytes(b)


def mutate_sidecar(rng, blob, page_size):
    b = bytearray(blob)
    if len(b) < 32:
        return bytes(b)
    if blob[:4] in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
        frames = max(1, (len(b) - 32) // (24 + page_size))
        off = 32 + rng.randrange(frames) * (24 + page_size) + rng.choice((0, 4))
        b[off:off + 4] = rng.choice((0, 1, 0xFFFFFFFF, 0x7FFFFFFF, 1000000)).to_bytes(4, "big")
    else:
        off = rng.choice((8, 12, 16, 20, 24))
        b[off:off + 4] = rng.choice((0, 1, 0xFFFFFFFF, 0x7FFFFFFF, 1000000, 512)).to_bytes(4, "big")
    return bytes(b)


# -- targets --------------------------------------------------------------------------------
def blob_target(target):
    """(function, exception classes it may raise) for a blob target."""
    from engine import decode, sqlsafe
    from engine.decode import compress, lz, zstd
    from engine.decode.core import Context
    from engine.fileformat import record
    from engine.xmlpretty import pretty_xml
    if target == "decode_blob":
        return decode.decode_blob, ()
    if target == "summary":
        return decode.summary, ()
    if target == "interpretations":
        return decode.interpretations, ()
    if target == "decoded_strings":
        return decode.decoded_strings, ()
    if target.startswith("lz:"):
        kind = target[3:]
        return (lambda d: lz.decompress(d, kind, 64 << 20)), ()
    if target == "zstd":
        return (lambda d: zstd.decompress(d, 64 << 20)), ()
    if target.startswith("compress:"):
        kind = target[9:]
        return (lambda d: compress.decode(d, Context(), 0, kind)), ()
    if target == "record":
        return record.decode_record_lenient, ()
    if target == "record_strict":
        return record.decode_record, (record.RecordError,)
    if target == "pretty_xml":
        return (lambda d: pretty_xml(d.decode("utf-8", "replace"))), (ValueError,)
    if target == "pretty_json":
        sys.path.insert(0, SRC)
        import value_viewer
        return (lambda d: value_viewer.pretty(d.decode("utf-8", "replace"))), (ValueError,)
    if target == "schema_text":
        from engine.schema import TableInfo, describe_table

        def run(d):
            text = d.decode("utf-8", "replace")
            sqlsafe.first_statement(text)
            sqlsafe.create_as_select(text)
            t = TableInfo("t", "virtual" if "VIRTUAL" in text.upper() else "table", 2, text)
            describe_table(t, set())
            with sqlsafe.Scratch() as s:
                try:
                    s.replay(text)
                except sqlsafe.ReplayError:
                    pass
        return run, ()
    raise SystemExit("unknown target %s" % target)


def seeds_for(target, extra=()):
    seeds = blob_seeds() + list(extra)
    if target in ("pretty_xml",):
        seeds = [s for s in seeds if s.lstrip()[:1] == b"<"] + [
            b"<a>" * 40 + b"x" + b"</a>" * 40, b"<?xml version='1.0'?><!DOCTYPE plist PUBLIC "
            b"'-//Apple//DTD PLIST 1.0//EN' 'x.dtd'><plist><dict/></plist>"]
    elif target == "pretty_json":
        seeds = [s for s in seeds if s[:1] in (b"{", b"[")] + [b"[" * 50 + b"]" * 50]
    elif target == "schema_text":
        seeds = [s.encode() for s in SCHEMA_SEEDS]
    elif target.startswith("record"):
        seeds = [b"\x04\x01\x0f\x00\x07abc", b"\x05\x18\x81\x01\x07" + bytes(80),
                 b"\x03\x01\x09\x05"]
    return seeds


def native_walk(path, deep):
    from engine.backends import NativeTable
    from engine.fileformat.freelist import freelist_pages, page_cells
    from engine.fileformat.header import HeaderError
    from engine.fileformat.pager import Pager, PageError
    from engine.fileformat.record import decode_record_lenient
    from engine.fileformat.wal import WalError, WalFile
    from engine.issues import IssueLog
    from engine.schema import SchemaModel
    issues = IssueLog(cap=1000)
    wal = None
    if os.path.exists(path + "-wal"):
        try:
            wal = WalFile(path + "-wal")
        except (WalError, OSError):
            wal = None
    try:
        pager = Pager(path, wal, issues=issues)
    except (HeaderError, PageError, ValueError, OSError):
        if wal is not None:
            wal.close()
        return
    try:
        entries = SchemaModel.read_master(pager, issues)
        model = SchemaModel(entries, issues)
        model.describe_all(None)
        for name in model.names():
            t = model.get(name)
            if t.natively_readable:
                nt = NativeTable(t, pager, issues)
                nt.count()
                nt.rows(0, 50)
                for k, _row in enumerate(nt.iter_all()):
                    if k > 2000:
                        break
        _trunks, leaves = freelist_pages(pager, issues)
        for leaf in leaves[:50]:
            for _k, _r, payload, _ref in page_cells(pager, leaf, issues):
                decode_record_lenient(payload, pager.encoding, issues)
        if deep:
            from engine.forensics.pages import PageMap
            PageMap(pager, entries, issues)
    finally:
        pager.close()
        if wal is not None:
            wal.close()


def session_walk(path, safe):
    from engine import session as S
    try:
        s = S.Session(path, hash_evidence=False, safe_parse=safe)
    except S.SessionError:
        return
    try:
        for name in (s.tables() + s.views())[:20]:
            s.browse(name, 0, 50)
            try:
                s.count(name)
            except S.SQL_ERRORS:
                pass                # a view SQLite cannot compute (reported in the page note)
        fx = s.forensics
        fx.audit(time_limit=5)
        fx.carve(time_limit=5)
        fx.dropped_schema(time_limit=2)
        if fx.journal() is not None:
            for e in fx.journal_schema()[:5]:
                fx.journal_rows(e.name, limit=50)
    finally:
        s.close(verify=False)


# -- running --------------------------------------------------------------------------------
def peak_mb():
    """Peak memory of this process in MB (working set or commit on Windows, maxrss on POSIX)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return max(pmc.PeakWorkingSetSize, pmc.PeakPagefileUsage) / 1048576.0
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1048576.0 if sys.platform == "darwin" else 1024.0)


def cap_memory(mb):
    """Cap this process's committed memory at mb MB (best effort)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes as wt

        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wt.DWORD),
                        ("f", ctypes.c_size_t), ("g", wt.DWORD), ("h", wt.DWORD)]

        class Ext(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("p", ctypes.c_size_t),
                        ("q", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wt.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = Ext()
        info.Basic.LimitFlags = 0x100           # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = mb << 20
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
        return
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (mb << 20, mb << 20))
    except (ImportError, ValueError, OSError):
        pass


class Finding(object):
    def __init__(self, kind, data, info, sides=None):
        self.kind, self.data, self.info, self.sides = kind, data, info, sides or {}


def run_inputs(target, seed, budget_s=None, count=None, slow=2.0, slow_db=25.0,
               mem_mb=None, on_input=None, extra_seeds=(), workdir=None):
    """Fuzz `target` in this process: until budget_s seconds or count inputs. Returns
    (inputs run, [Finding]). on_input(data, sides) is called before each input (the child
    saves it, so a hang can be kept). workdir: where database inputs are written (a fresh
    temp folder, removed afterwards, when None)."""
    rng = random.Random(seed)
    findings = []
    end = None if budget_s is None else time.time() + budget_s
    n = 0
    own_dir = workdir is None
    if own_dir:
        workdir = tempfile.mkdtemp(prefix="sga-fuzz-")
    else:
        os.makedirs(workdir, exist_ok=True)
    try:
        if target in DB_TARGETS:
            dbs = make_db_seeds(workdir)
        else:
            fn, allowed = blob_target(target)
            seeds = seeds_for(target, extra_seeds)
        while (end is None or time.time() < end) and (count is None or n < count):
            sides = {}
            if target in DB_TARGETS:
                _name, data, sides = rng.choice(dbs)
                sides = dict(sides)
                ps = struct.unpack(">H", data[16:18])[0]
                ps = 65536 if ps == 1 else (ps or 4096)
                if sides and rng.random() < 0.35:
                    side = rng.choice(sorted(sides))
                    sides[side] = (mutate(rng, sides[side]) if rng.random() < 0.5
                                   else mutate_sidecar(rng, sides[side], ps))
                else:
                    data = mutate_db(rng, data, ps)
            else:
                data = mutate(rng, rng.choice(seeds), seeds)
            if on_input is not None:
                on_input(data, sides)
            t0 = time.time()
            try:
                if target in DB_TARGETS:
                    path = _write_db(workdir, data, sides)
                    if target == "db_native":
                        native_walk(path, rng.random() < 0.3)
                    else:
                        session_walk(path, target == "db_safe")
                else:
                    fn(data)
            except MemoryError:
                findings.append(Finding("memory", data, traceback.format_exc(), sides))
            except Exception as e:      # noqa: BLE001 - anything else is a finding
                if target in DB_TARGETS or not isinstance(e, allowed):
                    findings.append(Finding("exc-" + type(e).__name__, data,
                                            traceback.format_exc(), sides))
            dt = time.time() - t0
            if dt > (slow_db if target in DB_TARGETS else slow):
                findings.append(Finding("slow", data, "%.2f s" % dt, sides))
            if mem_mb is not None and peak_mb() > mem_mb:
                findings.append(Finding("mem", data, "peak %.0f MB" % peak_mb(), sides))
                n += 1
                break
            n += 1
    finally:
        if own_dir:
            shutil.rmtree(workdir, ignore_errors=True)
    return n, findings


def _write_db(folder, data, sides):
    path = os.path.join(folder, "current.db")
    for side in ("", "-wal", "-journal", "-shm"):
        if os.path.exists(path + side):
            os.remove(path + side)
    with open(path, "wb") as f:
        f.write(data)
    for side, blob in sides.items():
        with open(path + side, "wb") as f:
            f.write(blob)
    return path


def save_finding(out, target, f):
    h = hashlib.sha1(f.data).hexdigest()[:12]
    base = os.path.join(out, "%s_%s_%s" % (target.replace(":", "-"), f.kind, h))
    with open(base + ".bin", "wb") as fh:
        fh.write(f.data)
    for side, blob in f.sides.items():
        with open(base + ".bin" + side, "wb") as fh:
            fh.write(blob)
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(f.info)
    return base


def child(args):
    """One target in this (child) process; prints 'ITER n FINDS k' at the end."""
    cap_memory(args.mem * 2)
    work = os.path.join(args.out, "work-" + args.target.replace(":", "-"))
    os.makedirs(work, exist_ok=True)
    current = os.path.join(work, "current.input")

    def keep(data, sides):
        with open(current, "wb") as fh:
            fh.write(data)

    n, found = run_inputs(args.target, args.seed, budget_s=args.seconds, slow=args.slow,
                          slow_db=args.slow_db, mem_mb=args.mem, on_input=keep,
                          workdir=os.path.join(work, "db"))
    for f in found:
        save_finding(args.out, args.target, f)
    print("ITER %d FINDS %d" % (n, len(found)))
    sys.stdout.flush()
    return 3 if any(f.kind == "mem" for f in found) else 0


DB_WEIGHT = 4               # a database target gets this many shares of the time (slower inputs)


def parent(args, targets):
    weights = [DB_WEIGHT if t in DB_TARGETS else 1 for t in targets]
    total_found = 0
    for k, target in enumerate(targets):
        per = args.seconds * weights[k] / float(sum(weights))
        deadline = time.time() + per
        runs, iters, events = 0, 0, []
        while time.time() < deadline - 1:
            left = deadline - time.time()
            runs += 1
            seed = args.seed * 1000003 + k * 7919 + runs
            cmd = [sys.executable, os.path.abspath(__file__), "--child", target,
                   "--seconds", "%.1f" % left, "--seed", str(seed), "--out", args.out,
                   "--mem", str(args.mem), "--slow", str(args.slow), "--slow-db",
                   str(args.slow_db)]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=left + 60,
                                   env=env)
                tail = [ln for ln in r.stdout.splitlines() if ln.startswith("ITER")]
                if tail:
                    iters += int(tail[-1].split()[1])
                if r.returncode == 3:
                    events.append("memory")
                elif r.returncode != 0:
                    events.append("crash rc=%d" % r.returncode)
                    _keep_current(args.out, target, "crash", r.stderr[-4000:])
            except subprocess.TimeoutExpired:
                events.append("hang")
                _keep_current(args.out, target, "hang", "no answer within %.0f s" % (left + 60))
        found = [n for n in os.listdir(args.out)
                 if n.startswith(target.replace(":", "-") + "_") and n.endswith(".txt")]
        total_found += len(found)
        print("%-18s runs=%d inputs=%d events=%s findings=%d" % (target, runs, iters, events,
                                                                   len(found)))
        sys.stdout.flush()
    print("TOTAL findings=%d out=%s" % (total_found, args.out))
    return 1 if total_found else 0


def _keep_current(out, target, kind, info):
    cur = os.path.join(out, "work-" + target.replace(":", "-"), "current.input")
    data = b""
    if os.path.exists(cur):
        with open(cur, "rb") as fh:
            data = fh.read()
    save_finding(out, target, Finding(kind, data, info))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--mem", type=int, default=500, help="peak MB counted as a finding")
    ap.add_argument("--slow", type=float, default=2.0)
    ap.add_argument("--slow-db", type=float, default=25.0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(TARGETS))
        return 0
    if args.minutes:
        args.seconds = args.minutes * 60.0
    if args.out is None:
        args.out = tempfile.mkdtemp(prefix="sga-fuzz-out-")
    os.makedirs(args.out, exist_ok=True)
    if args.child:
        args.target = args.child
        return child(args)
    targets = args.targets or list(TARGETS)
    for t in targets:
        if t not in TARGETS:
            ap.error("unknown target %s (see --list)" % t)
    return parent(args, targets)


if __name__ == "__main__":
    sys.exit(main())
