"""Fixture database for the timeline: dates in every common encoding, columns that only look
like dates (row ids, counters, phone numbers, sizes) and, optionally, a WAL holding an older
version of a dated row and a row deleted since.

Built at test time into a caller-supplied directory; the path of the main file is returned.
"""

import os
import shutil
import sqlite3
from datetime import datetime, timedelta

T0 = datetime(2021, 3, 4, 5, 6, 7)
STEP = timedelta(hours=3, minutes=17, seconds=11)
ROWS = 60
# Cocoa nanoseconds of Nov 2020 - May 2021 are also .NET ticks of 1995-2040 (the detector then
# prefers .NET): the Cocoa ns column holds dates two years later
COCOA_NS_SHIFT = timedelta(days=730)

_EPOCHS = {"unix": datetime(1970, 1, 1), "cocoa": datetime(2001, 1, 1),
           "win": datetime(1601, 1, 1), "hfs": datetime(1904, 1, 1), "net": datetime(1, 1, 1),
           "ole": datetime(1899, 12, 30), "gps": datetime(1980, 1, 6)}


def micros(dt, epoch):
    return (dt - _EPOCHS[epoch]) // timedelta(microseconds=1)


def when(i):
    """The date of row i (0-based)."""
    return T0 + i * STEP


def jitter(i):
    """Seconds added to the undated-name column 'plain.v': real dates do not step evenly."""
    return (i * i * 37) % 1000


def _rows():
    return [(i, when(i)) for i in range(ROWS)]


def build(directory, name="timeline.db", wal=True):
    path = os.path.join(directory, name)
    work = os.path.join(directory, "_tl_work")
    os.makedirs(work, exist_ok=True)
    wpath = os.path.join(work, name)
    for sfx in ("", "-wal", "-shm", "-journal"):
        for p in (path + sfx, wpath + sfx):
            if os.path.exists(p):
                os.remove(p)
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=4096")
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("BEGIN")
    c.execute("CREATE TABLE messages(_id INTEGER PRIMARY KEY, key_remote_jid TEXT, data TEXT, "
              "timestamp INTEGER, send_time INTEGER, phone INTEGER, media_size INTEGER, "
              "counter INTEGER, lat REAL)")
    c.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
              "last_visit_time INTEGER)")
    c.execute("CREATE TABLE ZNOTE(Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZCREATIONDATE REAL, "
              "ZMODIFICATIONDATE INTEGER)")
    c.execute("CREATE TABLE files(id INTEGER PRIMARY KEY, name TEXT, mtime INTEGER, "
              "created_ticks INTEGER, backup_date INTEGER)")
    c.execute("CREATE TABLE sheet(id INTEGER PRIMARY KEY, label TEXT, ole_date REAL, "
              "us_time INTEGER, ns_time INTEGER, gps_time INTEGER)")
    c.execute("CREATE TABLE notes(k TEXT PRIMARY KEY, body TEXT, created TEXT, modified TEXT) "
              "WITHOUT ROWID")
    c.execute("CREATE TABLE plain(id INTEGER PRIMARY KEY, what TEXT, v INTEGER)")
    c.execute("CREATE TABLE traps(id INTEGER PRIMARY KEY, contact TEXT, mobile INTEGER, "
              "phone_number INTEGER, file_size INTEGER, item_count INTEGER, score REAL, "
              "used INTEGER, flag INTEGER)")
    for i, d in _rows():
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                  (i + 1, "%d@s.whatsapp.net" % (919800000000 + i), "message %d" % i,
                   micros(d, "unix") // 1000, micros(d, "unix") // 1000000 + 30,
                   919876500000 + i, 1000 + i * 7919, i + 1, 12.5 + i / 100.0))
        c.execute("INSERT INTO urls VALUES (?,?,?,?)",
                  (i + 1, "https://example.org/%d" % i, "Page %d" % i,
                   0 if i % 10 == 9 else micros(d, "win")))
        c.execute("INSERT INTO ZNOTE VALUES (?,?,?,?)",
                  (i + 1, "note %d" % i, micros(d, "cocoa") / 1e6,
                   micros(d + COCOA_NS_SHIFT, "cocoa") * 1000))
        c.execute("INSERT INTO files VALUES (?,?,?,?,?)",
                  (i + 1, "file%d.txt" % i, micros(d, "win") * 10, micros(d, "net") * 10,
                   micros(d, "hfs") // 1000000))
        c.execute("INSERT INTO sheet VALUES (?,?,?,?,?,?)",
                  (i + 1, "cell %d" % i, micros(d, "ole") / 86400e6, micros(d, "unix"),
                   micros(d, "unix") * 1000, micros(d, "gps") // 1000000))
        c.execute("INSERT INTO notes VALUES (?,?,?,?)",
                  ("n%03d" % i, "body %d" % i, d.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   (d + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S") + "+02:00"))
        c.execute("INSERT INTO plain VALUES (?,?,?)",
                  (i + 1, "thing %d" % i, micros(d, "unix") // 1000000 + jitter(i)))
        c.execute("INSERT INTO traps VALUES (?,?,?,?,?,?,?,?,?)",
                  (i + 1, "+91 98765 %05d" % i, 9876543210 + i * 1111, 919876543210 + i,
                   (i * 7919 * 104729) % 4000000000, i * 3, 40000.5 + i, 10 ** (i % 10) + i,
                   i % 2))
    c.execute("COMMIT")
    if wal:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # two commits on the same page: the first one's copy is superseded by the second
        c.execute("UPDATE messages SET data = 'edited once', timestamp = timestamp + 1000 "
                  "WHERE _id = 5")
        c.execute("UPDATE messages SET data = 'edited twice', timestamp = timestamp + 2000 "
                  "WHERE _id = 5")
        c.execute("DELETE FROM messages WHERE _id = 7")
        for sfx in ("", "-wal"):
            shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    if not wal:
        shutil.copyfile(wpath, path)
    shutil.rmtree(work, ignore_errors=True)
    return path


# (table, column) -> kind the detector must find
EXPECTED = {("messages", "timestamp"): "unix_ms", ("messages", "send_time"): "unix_s",
            ("urls", "last_visit_time"): "webkit_us", ("ZNOTE", "ZCREATIONDATE"): "cocoa_s",
            ("ZNOTE", "ZMODIFICATIONDATE"): "cocoa_ns", ("files", "mtime"): "filetime",
            ("files", "created_ticks"): "dotnet_ticks", ("files", "backup_date"): "hfs_s",
            ("sheet", "ole_date"): "ole_days", ("sheet", "us_time"): "unix_us",
            ("sheet", "ns_time"): "unix_ns", ("notes", "created"): "iso_text",
            ("notes", "modified"): "iso_text", ("plain", "v"): "unix_s"}
# columns that must never be taken for dates
TRAPS = (("messages", "_id"), ("messages", "phone"), ("messages", "media_size"),
         ("messages", "counter"), ("messages", "lat"), ("messages", "key_remote_jid"),
         ("traps", "contact"), ("traps", "mobile"), ("traps", "phone_number"),
         ("traps", "file_size"), ("traps", "item_count"), ("traps", "score"), ("traps", "used"),
         ("traps", "flag"))
