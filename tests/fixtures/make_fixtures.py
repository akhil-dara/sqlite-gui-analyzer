"""Deterministic fixture databases, built at test time into a caller-supplied directory.

Every builder returns the path of the main database file. Builders that need a
live WAL copy the files while the writer connection is still open, so SQLite
never gets the chance to checkpoint them away.
"""

import os
import shutil
import sqlite3
import struct

from engine.fileformat.btree import TABLE_LEAF, cell_pointers, parse_page_header
from engine.fileformat.freelist import freelist_pages
from engine.fileformat.pager import Pager
from engine.fileformat.record import read_varint
from engine.fileformat.wal import wal_checksum, WAL_HEADER_SIZE, FRAME_HEADER_SIZE


def _fresh(directory, name, page_size=4096, encoding=None):
    path = os.path.join(directory, name)
    for sfx in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + sfx):
            os.remove(path + sfx)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=%d" % page_size)
    if encoding:
        conn.execute("PRAGMA encoding='%s'" % encoding)
    return path, conn


def without_rowid(directory):
    """PK declared last, composite PK, BLOB PK, DESC PK; enough rows for interior pages."""
    path, c = _fresh(directory, "without_rowid.db", page_size=1024)
    c.execute("CREATE TABLE pk_last(a TEXT, b INTEGER, c TEXT, PRIMARY KEY(c, a)) WITHOUT ROWID")
    c.execute("CREATE TABLE blob_pk(id BLOB PRIMARY KEY, v TEXT) WITHOUT ROWID")
    c.execute("CREATE TABLE desc_pk(k TEXT, v INTEGER, PRIMARY KEY(k DESC)) WITHOUT ROWID")
    c.executemany("INSERT INTO pk_last VALUES (?,?,?)",
                  [("a%04d" % i, i, "c%03d" % (i % 97)) for i in range(2000)])
    c.executemany("INSERT INTO blob_pk VALUES (?,?)",
                  [(bytes([i % 256, (i * 7) % 256, 0xFF]) + str(i).encode(), "v%d" % i) for i in range(500)])
    c.executemany("INSERT INTO desc_pk VALUES (?,?)", [("key%04d" % i, i) for i in range(800)])
    c.commit()
    c.close()
    return path


def overflow(directory, page_size):
    """Rows whose payload is 1x..5x the page size (TEXT and BLOB), plus small rows around them."""
    path, c = _fresh(directory, "overflow_%d.db" % page_size, page_size=page_size)
    c.execute("CREATE TABLE big(id INTEGER PRIMARY KEY, t TEXT, b BLOB)")
    rows = []
    for i, mult in enumerate((0.1, 1, 1.5, 2, 3, 5)):
        n = int(page_size * mult)
        rows.append((i + 1, ("x%d-" % i) * (n // 4 + 1), bytes((j * 31 + i) % 256 for j in range(n))))
    c.executemany("INSERT INTO big VALUES (?,?,?)", rows)
    c.execute("CREATE TABLE big_wr(k TEXT PRIMARY KEY, v BLOB) WITHOUT ROWID")
    c.executemany("INSERT INTO big_wr VALUES (?,?)",
                  [("k%d" % i, bytes(range(256)) * (page_size * (i + 1) // 256)) for i in range(4)])
    c.commit()
    c.close()
    return path


def encoded(directory, encoding):
    """UTF-16le / UTF-16be database with non-ASCII text."""
    path, c = _fresh(directory, "enc_%s.db" % encoding.replace("-", ""), encoding=encoding)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
    c.executemany("INSERT INTO t(s) VALUES (?)", [("Ünïcødé %d ✓ 漢字" % i,) for i in range(300)])
    c.commit()
    c.close()
    return path


def quirks(directory):
    """Shadowed rowid names, invalid UTF-8, wide table, view, ALTER defaults, generated columns,
    INTEGER PRIMARY KEY DESC (not a rowid alias), custom collation."""
    path, c = _fresh(directory, "quirks.db")
    c.create_collation("MYCOLL", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
    c.execute("CREATE TABLE shadow(rowid TEXT, _rowid_ TEXT, oid TEXT, v TEXT)")
    c.execute("INSERT INTO shadow VALUES ('r', 'u', 'o', 'shadowed')")
    c.execute("CREATE TABLE partial_shadow(rowid TEXT, v TEXT)")
    c.execute("INSERT INTO partial_shadow VALUES ('fake', 'real row')")
    c.execute("CREATE TABLE bad_text(id INTEGER PRIMARY KEY, s TEXT)")
    c.execute("INSERT INTO bad_text(s) VALUES ('good')")
    c.execute("INSERT INTO bad_text(s) VALUES (CAST(X'fffe41' AS TEXT))")
    cols = ", ".join("c%d TEXT" % i for i in range(250))
    c.execute("CREATE TABLE wide(%s)" % cols)
    c.execute("INSERT INTO wide VALUES (%s)" % ",".join("?" * 250), ["v%d" % i for i in range(250)])
    c.execute("CREATE VIEW v_shadow AS SELECT v FROM shadow")
    c.execute("CREATE TABLE altered(id INTEGER PRIMARY KEY, a TEXT)")
    c.execute("INSERT INTO altered(a) VALUES ('old row')")
    c.execute("ALTER TABLE altered ADD COLUMN b TEXT DEFAULT 'dflt'")
    c.execute("INSERT INTO altered(a, b) VALUES ('new row', 'set')")
    c.execute("CREATE TABLE ipk_desc(x INTEGER PRIMARY KEY DESC, y TEXT)")
    c.execute("INSERT INTO ipk_desc VALUES (10, 'ten')")
    c.execute("CREATE TABLE collated(name TEXT COLLATE MYCOLL, n INTEGER)")
    c.executemany("INSERT INTO collated VALUES (?,?)", [("b", 1), ("A", 2), ("c", 3)])
    c.execute("CREATE TABLE real_aff(id INTEGER PRIMARY KEY, r REAL)")
    c.execute("INSERT INTO real_aff(r) VALUES (2.0)")
    if sqlite3.sqlite_version_info >= (3, 31, 0):
        c.execute("CREATE TABLE gen(a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL, "
                  "c INTEGER GENERATED ALWAYS AS (a + 1) STORED, d TEXT)")
        c.execute("INSERT INTO gen(a, d) VALUES (5, 'five')")
    c.commit()
    c.close()
    return path


def views(directory):
    """A view over 100 rows whose name order is the reverse of their id order, plus an
    unrelated table with different columns (so a stale 'last browsed page' would show)."""
    path, c = _fresh(directory, "views.db")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO t(name) VALUES (?)", [("n%03d" % (100 - i),) for i in range(100)])
    c.execute("CREATE VIEW vw AS SELECT id, name FROM t")
    c.execute("CREATE TABLE other(w, x, y, z)")
    c.execute("INSERT INTO other VALUES (1, 2, 3, 4)")
    c.commit()
    c.close()
    return path


def wal_states(directory):
    """WAL holding committed frames (current + superseded), an uncommitted tail and stale frames.

    Sequence: commit 3000 rows (~150 frames) -> checkpoint RESTART -> commit 3 small
    transactions (restart the WAL with new salts; old frames past them are now stale) ->
    begin a big transaction with a 1-page cache so dirty pages spill to the WAL uncommitted ->
    copy db + wal while that transaction is open.
    """
    path = os.path.join(directory, "wal_states.db")
    work = os.path.join(directory, "_wal_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "wal_states.db")
    for sfx in ("", "-wal", "-shm"):
        for p in (path + sfx, wpath + sfx):
            if os.path.exists(p):
                os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
    c.execute("CREATE TABLE wr(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
    c.execute("BEGIN")
    c.executemany("INSERT INTO t(s) VALUES (?)", [("first-gen %d " % i * 3,) for i in range(3000)])
    c.execute("COMMIT")
    c.execute("PRAGMA wal_checkpoint(RESTART)")
    c.execute("INSERT INTO t(s) VALUES ('after restart 1')")
    c.execute("INSERT INTO wr VALUES ('key1', 'committed in wal')")
    c.execute("UPDATE t SET s = 'after restart 2' WHERE id = 3001")
    c.execute("PRAGMA cache_size=1")
    c.execute("BEGIN")
    c.executemany("INSERT INTO t(s) VALUES (?)", [("uncommitted %d " % i * 20,) for i in range(60)])
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.execute("ROLLBACK")
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path


def wal_only_data(directory):
    """Main file holds only the first page; every table and row lives in the WAL."""
    path = os.path.join(directory, "wal_only.db")
    work = os.path.join(directory, "_walonly_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "wal_only.db")
    for p in (path, path + "-wal", wpath, wpath + "-wal", wpath + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE activity(id BLOB PRIMARY KEY, payload TEXT) WITHOUT ROWID")
    c.executemany("INSERT INTO activity VALUES (?,?)",
                  [(bytes([i]) * 16, '{"app":"x%d"}' % i) for i in range(40)])
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path


def big_endian_wal(directory):
    """Copy of wal_states with magic 0x377f0683: checksum words big-endian, all checksums redone."""
    src_dir = os.path.join(directory, "_be_src")
    os.makedirs(src_dir, exist_ok=True)
    src = wal_states(src_dir)
    path = os.path.join(directory, "wal_be.db")
    shutil.copyfile(src, path)
    with open(src + "-wal", "rb") as f:
        data = bytearray(f.read())
    page_size = struct.unpack_from(">I", data, 8)[0]
    struct.pack_into(">I", data, 0, 0x377F0683)
    s0, s1 = wal_checksum(bytes(data[:24]), 0, 0, True)
    struct.pack_into(">II", data, 24, s0, s1)
    salts = bytes(data[16:24])
    off = WAL_HEADER_SIZE
    while off + FRAME_HEADER_SIZE + page_size <= len(data):
        if bytes(data[off + 8:off + 16]) != salts:
            break                       # keep stale frames stale
        s0, s1 = wal_checksum(bytes(data[off:off + 8]), s0, s1, True)
        s0, s1 = wal_checksum(bytes(data[off + 24:off + 24 + page_size]), s0, s1, True)
        struct.pack_into(">II", data, off + 16, s0, s1)
        off += FRAME_HEADER_SIZE + page_size
    with open(path + "-wal", "wb") as f:
        f.write(bytes(data))
    return path


def corrupt(directory):
    """A table whose root page type byte is destroyed; a second table stays healthy."""
    path, c = _fresh(directory, "corrupt.db", page_size=1024)
    c.execute("CREATE TABLE ok(id INTEGER PRIMARY KEY, s TEXT)")
    c.execute("CREATE TABLE broken(id INTEGER PRIMARY KEY, s TEXT)")
    c.executemany("INSERT INTO ok(s) VALUES (?)", [("fine %d" % i,) for i in range(50)])
    c.executemany("INSERT INTO broken(s) VALUES (?)", [("lost %d" % i,) for i in range(50)])
    c.commit()
    root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='broken'").fetchone()[0]
    c.close()
    with open(path, "r+b") as f:
        f.seek((root - 1) * 1024)
        f.write(b"\x3d")
    return path


def freelist(directory):
    """Deleted rows left on freelist leaf pages (secure_delete off, no vacuum)."""
    path, c = _fresh(directory, "freelist.db", page_size=1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    c.executemany("INSERT INTO notes(body) VALUES (?)", [("note number %d " % i * 4,) for i in range(400)])
    c.commit()
    c.execute("DELETE FROM notes WHERE id > 20")
    c.commit()
    c.close()
    return path


def _record_header_pos(page):
    """Offset of the record-header-length byte of the first cell on a table-leaf page image."""
    h = parse_page_header(page, 0)
    off = cell_pointers(page, h, len(page))[0]
    _, p = read_varint(page, off)       # payload length
    _, p = read_varint(page, p)         # rowid
    return p


def _zero_record_header(path, page_no, page_size):
    """Write 0 into the record-header-length byte of the first cell on page page_no."""
    with open(path, "r+b") as f:
        f.seek((page_no - 1) * page_size)
        pos = _record_header_pos(f.read(page_size))
        f.seek((page_no - 1) * page_size + pos)
        f.write(b"\x00")


def damaged_records(directory, name="damaged.db", generated=False):
    """Records whose header length is 0 (an all-NULL row, flagged damaged_record):
    the only row of 'ev' in the main file, and a row of 'gone' on a freelist leaf page. Both
    tables have DEFAULTs (one non-constant), which must not be shown for a damaged record.
    generated=True adds a table with a VIRTUAL generated column."""
    path, c = _fresh(directory, name, page_size=1024)
    if generated and sqlite3.sqlite_version_info >= (3, 31, 0):
        c.execute("CREATE TABLE gen(a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL)")
        c.execute("INSERT INTO gen(a) VALUES (21)")
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE ev(id INTEGER PRIMARY KEY, status TEXT DEFAULT 'deleted', "
              "created TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.execute("INSERT INTO ev VALUES (1, 'active', '2019-01-01 10:00:00')")
    c.execute("CREATE TABLE gone(id INTEGER PRIMARY KEY, status TEXT DEFAULT 'deleted', "
              "body TEXT, extra TEXT)")
    c.executemany("INSERT INTO gone(status, body, extra) VALUES ('active', ?, 'e')",
                  [("gone row %d " % i * 4,) for i in range(200)])
    c.commit()
    c.execute("DELETE FROM gone WHERE id > 5")
    c.commit()
    ev_root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='ev'").fetchone()[0]
    c.close()
    pager = Pager(path)
    try:
        _trunks, leaves = freelist_pages(pager)
        leaf = next(n for n in leaves if pager.page(n)[0] == TABLE_LEAF)
    finally:
        pager.close()
    _zero_record_header(path, ev_root, 1024)
    _zero_record_header(path, leaf, 1024)
    return path


def unreadable_by_sqlite(directory):
    """damaged_records (plus a VIRTUAL generated column) with header bytes 21-23 (the payload
    fractions, always 64/32/32) cleared: SQLite refuses the file ('file is not a database'), the
    native reader does not, so every table is read natively (NATIVE mode) with its row flags."""
    path = damaged_records(directory, "native_only.db", generated=True)
    with open(path, "r+b") as f:
        f.seek(21)
        f.write(b"\x00\x00\x00")
    return path


def damaged_wal_record(directory):
    """A WAL frame of 'ev' whose record header length is 0. The frame then fails its checksum
    (state 'uncommitted'); its row must still be shown, all-NULL and flagged damaged_record."""
    path = os.path.join(directory, "damaged_wal.db")
    work = os.path.join(directory, "_dwal_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "damaged_wal.db")
    for p in (path, path + "-wal", wpath, wpath + "-wal", wpath + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE ev(id INTEGER PRIMARY KEY, status TEXT DEFAULT 'deleted', note TEXT)")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("INSERT INTO ev VALUES (1, 'active', 'in wal')")
    root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='ev'").fetchone()[0]
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    with open(path + "-wal", "rb") as f:
        data = bytearray(f.read())
    page_size = struct.unpack_from(">I", data, 8)[0]
    off = WAL_HEADER_SIZE
    while off + FRAME_HEADER_SIZE + page_size <= len(data):
        if struct.unpack_from(">I", data, off)[0] == root:
            start = off + FRAME_HEADER_SIZE
            data[start + _record_header_pos(bytes(data[start:start + page_size]))] = 0
            break
        off += FRAME_HEADER_SIZE + page_size
    with open(path + "-wal", "wb") as f:
        f.write(bytes(data))
    return path


MIXED_A = [
    None, -5, 0, 5, 10, 99999999999, 2.5, 0.1 + 0.2, 1e20, -0.0, 5.0, 1 / 3.0,
    "5", "10", "abc", "ABC", "aBc", "Ünïcødé", "ü", "Ü", "a\x00bc", "", "PROGRA~1",
    "C:\\Users\\x", "50%", "user_id", "userXid", "line1\nline2", 'say "hi"', "it's", " pad ",
    "2020-06-01", b"\x00abc", b"abc", b"\xff\xfe", b"", b"5", "zzz", "é", "a_c",
    "Āb", "字", "ſx",       # above U+00FF: UTF-16LE byte order differs from code point order
]


def mixed_values(directory, encoding=None):
    """Values of every storage class for filter tests. 'vals' has one row per MIXED_A value in
    the untyped column a, beside TEXT, INTEGER and REAL columns (affinity applied on insert),
    plus a row of invalid UTF-8 text. 'nn' declares NOT NULL columns that hold NULLs (as a
    damaged or altered file can): SQLite must not optimise 'IS NULL' away for them."""
    name = "mixed.db" if not encoding else "mixed_%s.db" % encoding.replace("-", "")
    path, c = _fresh(directory, name, encoding=encoding)
    c.execute("CREATE TABLE vals(id INTEGER PRIMARY KEY, a, t TEXT, i INTEGER, r REAL)")
    ts = ["x", "5", "abc", None, "Zeta", "10", "é", "a\x00z", 7]
    ints = [0, 5, -3, None, 12, "abc", 1 << 40, 3.5]
    reals = [0.0, 2.5, -1.5, None, 1e-7, 100.0, 12345.678, "n/a"]
    rows = [(v, ts[k % len(ts)], ints[k % len(ints)], reals[k % len(reals)])
            for k, v in enumerate(MIXED_A)]
    c.executemany("INSERT INTO vals(a, t, i, r) VALUES (?,?,?,?)", rows)
    if not encoding:
        c.execute("INSERT INTO vals(a, t) VALUES (CAST(X'fffe41' AS TEXT), CAST(X'41ff' AS TEXT))")
    c.execute("CREATE TABLE nn(k INTEGER, v TEXT)")
    c.executemany("INSERT INTO nn VALUES (?,?)", [(1, "one"), (None, "missing"), (3, None)])
    c.commit()
    c.execute("PRAGMA writable_schema=ON")
    c.execute("UPDATE sqlite_master SET sql='CREATE TABLE nn(k INTEGER NOT NULL, v TEXT NOT NULL)' "
              "WHERE name='nn'")
    c.commit()
    c.close()
    return path


def wide(directory, columns=250, rows=3000):
    """A table of `columns` columns (integers, text, reals, NULLs and BLOBs) and `rows` rows."""
    path, c = _fresh(directory, "wide.db")
    decl = ["id INTEGER PRIMARY KEY"]
    for i in range(1, columns):
        decl.append(("c%03d %s" % (i, ("BLOB", "INTEGER", "TEXT", "REAL", "")[i % 5])).strip())
    c.execute("CREATE TABLE wide(%s)" % ", ".join(decl))
    marks = ",".join("?" * columns)

    def value(r, i):
        kind = i % 5
        if (r + i) % 23 == 0:
            return None
        if kind == 1:
            return (r * 7 + i) % 1000
        if kind == 2:
            return "row %d col %d %s" % (r, i, "x" * ((r + i) % 30))
        if kind == 3:
            return (r * 13 + i) / 8.0
        if kind == 4:
            return "mixed %d" % r if r % 2 else r
        return bytes(((r + i + j) % 256 for j in range((r + i) % 12)))
    c.executemany("INSERT INTO wide VALUES (%s)" % marks,
                  [[r + 1] + [value(r, i) for i in range(1, columns)] for r in range(rows)])
    c.commit()
    c.close()
    return path


def relations(directory):
    """Tables linked by a declared FOREIGN KEY, by names only (message_row_id -> message._id,
    sender_jid_row_id -> jid._id), a misleading name whose values are nowhere (settings
    .message_id), TEXT against INTEGER (labels.chat_row_id), a WITHOUT ROWID target
    (device.device_id), an unindexed referring column (message_vote), a 0/1 column named like
    a key (status_id) and a table without INTEGER PRIMARY KEY (note, reached by its rowid)."""
    path, c = _fresh(directory, "relations.db")
    c.executescript("""
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, raw_string TEXT);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INTEGER, text_data TEXT,
                             status_id INTEGER);
        CREATE INDEX message_chat ON message(chat_row_id);
        CREATE TABLE message_poll(message_row_id INTEGER PRIMARY KEY, option_count INTEGER);
        CREATE TABLE message_poll_option(_id INTEGER PRIMARY KEY, message_row_id INTEGER,
                                         option_name TEXT);
        CREATE INDEX poll_option_message ON message_poll_option(message_row_id);
        CREATE TABLE message_vote(_id INTEGER PRIMARY KEY, message_row_id INTEGER,
                                  sender_jid_row_id INTEGER);
        CREATE TABLE receipt(_id INTEGER PRIMARY KEY, msg INTEGER REFERENCES message(_id),
                             state INTEGER);
        CREATE TABLE settings(_id INTEGER PRIMARY KEY, message_id INTEGER, label TEXT);
        CREATE TABLE labels(_id INTEGER PRIMARY KEY, chat_row_id TEXT, label TEXT);
        CREATE TABLE status(_id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE device(device_id TEXT PRIMARY KEY, model TEXT) WITHOUT ROWID;
        CREATE TABLE user_device(_id INTEGER PRIMARY KEY, device_id TEXT, jid_row_id INTEGER);
        CREATE TABLE note(body TEXT);
        CREATE TABLE note_link(note_id INTEGER, message_row_id INTEGER);
    """)
    c.executemany("INSERT INTO jid VALUES (?,?)",
                  [(i, "%d@s.example" % (1000 + i)) for i in range(1, 21)])
    c.executemany("INSERT INTO chat VALUES (?,?,?)",
                  [(i, i + 5, "chat %d" % i) for i in range(1, 11)])
    c.executemany("INSERT INTO message VALUES (?,?,?,?)",
                  [(i, 1 + i % 10, "message %d" % i, i % 2) for i in range(1, 301)])
    polls = list(range(5, 301, 5))
    c.executemany("INSERT INTO message_poll VALUES (?,?)", [(m, 3) for m in polls])
    c.executemany("INSERT INTO message_poll_option(message_row_id, option_name) VALUES (?,?)",
                  [(m, "option %d.%d" % (m, k)) for m in polls for k in range(3)])
    c.executemany("INSERT INTO message_vote(message_row_id, sender_jid_row_id) VALUES (?,?)",
                  [(m, 1 + (m + k) % 20) for m in polls for k in range(2)])
    c.executemany("INSERT INTO receipt(msg, state) VALUES (?,?)",
                  [(i, i % 3) for i in range(1, 301, 3)])
    c.executemany("INSERT INTO settings(message_id, label) VALUES (?,?)",
                  [(900000 + i, "setting %d" % i) for i in range(40)])
    c.executemany("INSERT INTO labels(chat_row_id, label) VALUES (?,?)",
                  [(str(1 + i % 10), "label %d" % i) for i in range(30)])
    c.executemany("INSERT INTO status VALUES (?,?)", [(i, "state %d" % i) for i in range(6)])
    c.executemany("INSERT INTO device VALUES (?,?)",
                  [("dev-%02d" % i, "model %d" % i) for i in range(12)])
    c.executemany("INSERT INTO user_device(device_id, jid_row_id) VALUES (?,?)",
                  [("dev-%02d" % (i % 12), 1 + i % 20) for i in range(40)])
    c.executemany("INSERT INTO note(body) VALUES (?)", [("note %d" % i,) for i in range(15)])
    c.executemany("INSERT INTO note_link VALUES (?,?)",
                  [(1 + i % 15, 5 * (1 + i)) for i in range(30)])
    c.commit()
    c.close()
    return path


def values_everywhere(directory):
    """One value in columns of every type: the number 5 as INTEGER, TEXT '5' and BLOB '5';
    the bytes token-123 as a whole BLOB, as TEXT and inside larger BLOBs."""
    path, c = _fresh(directory, "values.db")
    c.executescript("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, n INTEGER, t TEXT, b BLOB);
        CREATE TABLE c(id INTEGER PRIMARY KEY, code TEXT, data BLOB);
        CREATE VIEW v AS SELECT id, code FROM c;
    """)
    c.executemany("INSERT INTO a VALUES (?,?,?,?)",
                  [(1, 5, "five", b"\x00\x01token-123\x02"), (2, 7, "5", b"5"),
                   (3, 55, "x5x", b"token-123")])
    c.executemany("INSERT INTO c VALUES (?,?,?)",
                  [(1, "token-123", b"zz token-123 zz"), (2, "abc", b"\x00\x01token-123\x02")])
    c.commit()
    c.close()
    return path


def build_all(directory):
    """Build every fixture; returns {name: path}."""
    out = {"without_rowid": without_rowid(directory), "utf16le": encoded(directory, "UTF-16le"),
           "utf16be": encoded(directory, "UTF-16be"), "quirks": quirks(directory),
           "wal_states": wal_states(directory), "wal_only": wal_only_data(directory),
           "wal_be": big_endian_wal(directory), "corrupt": corrupt(directory),
           "freelist": freelist(directory), "mixed": mixed_values(directory),
           "wide": wide(directory)}
    for ps in (512, 4096, 16384, 65536):
        out["overflow_%d" % ps] = overflow(directory, ps)
    return out
