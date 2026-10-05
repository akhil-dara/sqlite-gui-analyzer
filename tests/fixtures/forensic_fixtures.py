"""Deterministic fixture databases for the forensics tests, built at test time into a
caller-supplied directory (never next to real evidence).

Every builder returns the path of the main database file. The row helpers (person(),
note(), item(), big_row(), account(), secret(), original()) give the exact values each
builder writes, so tests can check recovered rows value by value.
"""

import os
import random
import shutil
import sqlite3
import struct

from tests.fixtures.make_fixtures import _fresh

PEOPLE_SINGLE = (5, 17, 33, 120)          # deleted one by one: separate freeblocks
PEOPLE_RUN = (50, 51, 52)                 # deleted together: one coalesced freeblock
NOTES_DELETED = range(101, 301)           # whole leaf pages go to the freelist


def person(i):
    return (i, "person %d" % i, "p%d@example.com" % i, 20 + i % 50)


def note(i):
    return (i, ("note number %d " % i) * 4)


def deleted_rows(directory, secure_delete=False):
    """people (200 rows) with PEOPLE_SINGLE and PEOPLE_RUN deleted; notes (400 rows) with
    NOTES_DELETED deleted. secure_delete=True zeroes what the deletes free."""
    path, c = _fresh(directory, "deleted_sd.db" if secure_delete else "deleted.db", 1024)
    c.execute("PRAGMA secure_delete=%s" % ("ON" if secure_delete else "OFF"))
    c.execute("CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT, email TEXT, age INTEGER)")
    c.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    c.executemany("INSERT INTO people VALUES (?,?,?,?)", [person(i) for i in range(1, 201)])
    c.executemany("INSERT INTO notes VALUES (?,?)", [note(i) for i in range(1, 401)])
    c.commit()
    for i in PEOPLE_SINGLE:
        c.execute("DELETE FROM people WHERE id = ?", (i,))
    c.execute("DELETE FROM people WHERE id BETWEEN ? AND ?", (PEOPLE_RUN[0], PEOPLE_RUN[-1]))
    c.execute("DELETE FROM notes WHERE id BETWEEN ? AND ?", (NOTES_DELETED[0], NOTES_DELETED[-1]))
    c.commit()
    c.close()
    return path


LINES_DELETED = (1050, 1130, 1210)
PLAIN_DELETED = (30, 70, 110)


def line(i):
    return (i, "s%d" % i, "line text for record %d" % i, i)


def plain(i):
    return ("label number %d" % i, i * 3, i / 4.0)


def freeblock_rows(directory):
    """m: 2-byte rowids (1000..1259), LINES_DELETED removed (each its own freeblock).
    plain: a rowid table without INTEGER PRIMARY KEY (its first stored column is lost with
    the freeblock header and must be solved from the size), PLAIN_DELETED removed."""
    path, c = _fresh(directory, "freeblocks.db", 4096)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE m(id INTEGER PRIMARY KEY, sid TEXT, line TEXT, n INT)")
    c.executemany("INSERT INTO m VALUES (?,?,?,?)", [line(i) for i in range(1000, 1260)])
    c.execute("CREATE TABLE plain(label TEXT, qty INTEGER, ratio REAL)")
    c.executemany("INSERT INTO plain VALUES (?,?,?)", [plain(i) for i in range(1, 151)])
    c.commit()
    for i in LINES_DELETED:
        c.execute("DELETE FROM m WHERE id = ?", (i,))
    for i in PLAIN_DELETED:
        c.execute("DELETE FROM plain WHERE qty = ?", (i * 3,))
    c.commit()
    c.close()
    return path


ITEMS_SINGLE = (40, 90, 150)
ITEMS_RUN = range(200, 261)


def item(i):
    return ("key-%04d-%s" % (i, "k" * 20), ("value text for item %d " % i) * 5, i * 7)


def without_rowid_deleted(directory):
    """items(k TEXT PRIMARY KEY, v, n) WITHOUT ROWID, 300 rows of ~150 bytes; ITEMS_SINGLE
    deleted one by one (freeblocks), ITEMS_RUN deleted together (freed pages)."""
    path, c = _fresh(directory, "wr_deleted.db", 1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE items(k TEXT PRIMARY KEY, v TEXT, n INTEGER) WITHOUT ROWID")
    c.executemany("INSERT INTO items VALUES (?,?,?)", [item(i) for i in range(1, 301)])
    c.commit()
    for i in ITEMS_SINGLE:
        c.execute("DELETE FROM items WHERE k = ?", (item(i)[0],))
    c.execute("DELETE FROM items WHERE n BETWEEN ? AND ?", (ITEMS_RUN[0] * 7, ITEMS_RUN[-1] * 7))
    c.commit()
    c.close()
    return path


BIG_DELETED = (2, 4, 9)


def big_row(i):
    """A row whose payload is exactly 4064 bytes: on 1024-byte pages only 104 bytes stay on the
    leaf page (so several rows share a leaf) and the rest fills 4 overflow pages."""
    title = "title %02d" % i
    data = bytes((j * 7 + i) % 256 for j in range(2048))
    body = (("body of big row %d; " % i) * 200)[:4057 - len(title) - len(data)]
    return (i, title, body, data)


def overflow_deleted(directory, reuse=False):
    """big: 12 rows spilling onto overflow pages, BIG_DELETED removed (freeblocks on their
    leaf pages). A freelist trunk exists beforehand, so the freed overflow pages become
    freelist leaves and keep their content. reuse=True then fills another table until the
    freed pages are reused: the deleted rows' overflow chains are broken."""
    path, c = _fresh(directory, "overflow_reuse.db" if reuse else "overflow_del.db", 1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE filler(x TEXT)")
    c.executemany("INSERT INTO filler VALUES (?)", [("filler row %d " % i * 10,) for i in range(60)])
    c.execute("CREATE TABLE big(id INTEGER PRIMARY KEY, title TEXT, body TEXT, data BLOB)")
    c.executemany("INSERT INTO big VALUES (?,?,?,?)", [big_row(i) for i in range(1, 13)])
    c.commit()
    c.execute("DELETE FROM filler")
    c.commit()
    c.executemany("DELETE FROM big WHERE id = ?", [(i,) for i in BIG_DELETED])
    c.commit()
    if reuse:
        c.execute("CREATE TABLE later(x BLOB)")
        c.executemany("INSERT INTO later VALUES (?)", [(bytes([i]) * 900,) for i in range(200)])
        c.commit()
    c.close()
    return path


def account(i):
    return (i, "owner %d" % i, "open", 100 * i)


def wal_history(directory):
    """acct in WAL mode, baseline checkpointed into the main file, then (one commit each):
    row 5 updated three times, row 7 deleted, row 20 deleted, a different entity inserted as
    rowid 20. The db and WAL are copied while the writer is still open."""
    path = os.path.join(directory, "wal_history.db")
    work = os.path.join(directory, "_walhist_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "wal_history.db")
    for p in (path, path + "-wal", wpath, wpath + "-wal", wpath + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE acct(id INTEGER PRIMARY KEY, owner TEXT, status TEXT, balance INTEGER)")
    c.executemany("INSERT INTO acct VALUES (?,?,?,?)", [account(i) for i in range(1, 21)])
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("UPDATE acct SET balance = 555 WHERE id = 5")
    c.execute("UPDATE acct SET status = 'frozen' WHERE id = 5")
    c.execute("UPDATE acct SET status = 'open', balance = 777 WHERE id = 5")
    c.execute("DELETE FROM acct WHERE id = 7")
    c.execute("DELETE FROM acct WHERE id = 20")
    c.execute("INSERT INTO acct(owner, status, balance) VALUES ('someone else', 'new', 1)")
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path


def secret(i):
    return (i, "account %d" % i, "pw-%d-secret" % i)


def dropped_table(directory):
    """keep (2 rows) stays; secrets (150 rows over several pages, with an index) is dropped."""
    path, c = _fresh(directory, "dropped.db", 1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE keep(id INTEGER PRIMARY KEY, x TEXT)")
    c.executemany("INSERT INTO keep VALUES (?,?)", [(1, "a"), (2, "b")])
    c.execute("CREATE TABLE secrets(id INTEGER PRIMARY KEY, account TEXT, password TEXT)")
    c.executemany("INSERT INTO secrets VALUES (?,?,?)", [secret(i) for i in range(1, 151)])
    c.execute("CREATE INDEX secrets_account ON secrets(account)")
    c.commit()
    c.execute("DROP TABLE secrets")
    c.commit()
    c.close()
    return path


def original(i):
    return (i, "original text of row %d" % i)


def hot_journal(directory):
    """DELETE journal mode: 1500 rows committed; then an open transaction updates every row
    and deletes rows > 1000 with a tiny page cache, so changed pages spill into the main file.
    The db and its hot -journal are copied while the transaction is open."""
    path = os.path.join(directory, "hot.db")
    work = os.path.join(directory, "_hot_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "hot.db")
    for p in (path, path + "-journal", wpath, wpath + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
    c.execute("BEGIN")
    c.executemany("INSERT INTO t VALUES (?,?)", [original(i) for i in range(1, 1501)])
    c.execute("COMMIT")
    c.execute("PRAGMA cache_size=10")
    c.execute("BEGIN")
    c.execute("UPDATE t SET s = 'changed ' || id")
    c.execute("DELETE FROM t WHERE id > 1000")
    for sfx in ("", "-journal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.execute("ROLLBACK")
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path


def persist_journal(directory):
    """PERSIST journal mode: 300 rows committed, then every row updated in a second committed
    transaction. The journal header is zeroed at commit; its page records remain."""
    path = os.path.join(directory, "persist.db")
    for p in (path, path + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=PERSIST")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
    c.execute("BEGIN")
    c.executemany("INSERT INTO t VALUES (?,?)", [original(i) for i in range(1, 301)])
    c.execute("COMMIT")
    c.execute("BEGIN")
    c.execute("UPDATE t SET s = 'updated ' || id")
    c.execute("COMMIT")
    c.close()
    return path


def crafted_header(directory, name, patches=(), append=b""):
    """A small database (two tables, a freelist) with bytes overwritten at the given
    (offset, bytes) pairs and `append` added at the end of the file."""
    path, c = _fresh(directory, name, 1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE a(id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE b(id INTEGER PRIMARY KEY, w TEXT)")
    c.executemany("INSERT INTO a VALUES (?,?)", [(i, "a value %d " % i * 5) for i in range(1, 121)])
    c.executemany("INSERT INTO b VALUES (?,?)", [(i, "b value %d" % i) for i in range(1, 51)])
    c.commit()
    c.execute("DELETE FROM a WHERE id > 40")
    c.commit()
    c.close()
    with open(path, "r+b") as f:
        for off, data in patches:
            f.seek(off)
            f.write(data)
        if append:
            f.seek(0, 2)
            f.write(append)
    return path


def hostile(directory, seed=7, rate=0.04):
    """deleted_rows with a deterministic share of the bytes of every page after page 1
    replaced by random values, plus hand-made traps: a freeblock chain that loops, a cell
    claiming an enormous payload and an overflow chain that points to itself."""
    src_dir = os.path.join(directory, "_hostile_src_%d" % seed)
    os.makedirs(src_dir, exist_ok=True)
    src = deleted_rows(src_dir)
    path = os.path.join(directory, "hostile_%d.db" % seed)
    shutil.copyfile(src, path)
    rnd = random.Random(seed)
    with open(path, "rb") as f:
        data = bytearray(f.read())
    ps = 1024
    for i in range(ps, len(data)):
        if rnd.random() < rate:
            data[i] = rnd.randrange(256)
    # page 3 becomes a table leaf whose freeblock chain loops and whose cell claims an
    # enormous payload
    p3 = 2 * ps
    data[p3:p3 + 8] = bytes([0x0D, 0x02, 0x58, 0, 1, 0x03, 0x00, 0])
    struct.pack_into(">H", data, p3 + 8, 0x300)
    struct.pack_into(">HH", data, p3 + 0x258, 0x258, 40)             # next = itself
    data[p3 + 0x300:p3 + 0x309] = b"\xff" * 9
    # the last page becomes a table leaf whose single cell overflows into the page itself
    last = len(data) - ps
    data[last:last + 8] = bytes([0x0D, 0, 0, 0, 1, 0x03, 0x84, 0])
    struct.pack_into(">H", data, last + 8, 900)
    data[last + 900:last + 906] = b"\x87\x67\x01\x03\x8f\x55"      # payload 999, rowid 1
    struct.pack_into(">I", data, last + 903 + 104, len(data) // ps)
    with open(path, "wb") as f:
        f.write(bytes(data))
    return path


# -- index entries ----------------------------------------------------------------------------
MEMBERS_SINGLE = (5, 17, 33, 120)
MEMBERS_RUN = range(200, 261)
MEMBERS_DELETED = tuple(MEMBERS_SINGLE) + tuple(MEMBERS_RUN)
MEMBER_INDEXES = ("m_name", "m_age_name", "m_city_email", "sqlite_autoindex_members_1")


def member(i):
    return (i, "member %d" % i, "m%d@example.org" % i, 20 + i % 50, "city %d" % (i % 7))


def member_entry(index, i):
    """The entry index `index` holds for member i."""
    _id, name, email, age, city = member(i)
    return {"m_name": [name, i], "m_age_name": [age, name.lower(), i],
            "m_city_email": [city, email, i], "sqlite_autoindex_members_1": [email, i]}[index]


def indexed_deleted(directory, secure_delete=False):
    """members (400 rows) with a UNIQUE column, a plain index, a composite DESC index with an
    expression and a partial composite index (and a view, seniors); MEMBERS_DELETED removed."""
    path, c = _fresh(directory, "indexed_sd.db" if secure_delete else "indexed.db", 1024)
    c.execute("PRAGMA secure_delete=%s" % ("ON" if secure_delete else "OFF"))
    c.execute("CREATE TABLE members(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, "
              "age INTEGER, city TEXT)")
    c.execute("CREATE INDEX m_name ON members(name)")
    c.execute("CREATE INDEX m_age_name ON members(age DESC, lower(name))")
    c.execute("CREATE INDEX m_city_email ON members(city, email COLLATE NOCASE) "
              "WHERE age > 0")
    c.execute("CREATE VIEW seniors AS SELECT id, name, email FROM members WHERE age >= 60")
    c.executemany("INSERT INTO members VALUES (?,?,?,?,?)", [member(i) for i in range(1, 401)])
    c.commit()
    for i in MEMBERS_SINGLE:
        c.execute("DELETE FROM members WHERE id = ?", (i,))
    c.execute("DELETE FROM members WHERE id BETWEEN ? AND ?", (MEMBERS_RUN[0], MEMBERS_RUN[-1]))
    c.commit()
    c.close()
    return path


TAGGED_GONE = range(10, 21)


def doc(i):
    return (i, "tag-%03d" % i, ("body of document %d " % i) * 40)


def indexed_rows_gone(directory):
    """docs: one ~800-byte row per page, indexed by tag (several index pages). TAGGED_GONE
    are deleted, then new rows (whose tags sort after every old one, so their entries go to
    the last index page) reuse the freed table pages: the rows are gone, their index entries
    are not."""
    path, c = _fresh(directory, "indexed_gone.db", 1024)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, tag TEXT, body TEXT)")
    c.execute("CREATE INDEX docs_tag ON docs(tag)")
    c.executemany("INSERT INTO docs VALUES (?,?,?)", [doc(i) for i in range(1, 201)])
    c.commit()
    c.execute("DELETE FROM docs WHERE id BETWEEN ? AND ?", (TAGGED_GONE[0], TAGGED_GONE[-1]))
    c.commit()
    c.executemany("INSERT INTO docs VALUES (?,?,?)",
                  [(1000 + i, "zzz-%03d" % i, ("replacement text %d " % i) * 40)
                   for i in range(len(TAGGED_GONE))])
    c.commit()
    c.close()
    return path


def indexed_wal(directory):
    """members in WAL mode: the baseline is checkpointed into the main file, then (in the WAL,
    one commit each) rows 5 and 7 are deleted and row 9's name is changed. The db and WAL are
    copied while the writer is still open."""
    path = os.path.join(directory, "indexed_wal.db")
    work = os.path.join(directory, "_idxwal_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, "indexed_wal.db")
    for p in (path, path + "-wal", wpath, wpath + "-wal", wpath + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("PRAGMA secure_delete=ON")         # nothing in free space: only page copies
    c.execute("CREATE TABLE members(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, "
              "age INTEGER, city TEXT)")
    c.execute("CREATE INDEX m_name ON members(name)")
    c.executemany("INSERT INTO members VALUES (?,?,?,?,?)", [member(i) for i in range(1, 31)])
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("DELETE FROM members WHERE id = 5")
    c.execute("DELETE FROM members WHERE id = 7")
    c.execute("UPDATE members SET name = 'renamed 9' WHERE id = 9")
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path
