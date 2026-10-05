"""A realistic case of many databases, as a phone extraction lays them out: one folder per app
(com.whatsapp/databases, com.phonepe.app/databases, ...), files with and without an extension,
live WAL files, a hot rollback journal, empty databases, dates of several kinds, and values
shared between databases (phone numbers, jids, account ids) so links are found by value.
Built at test time into a caller-supplied directory; deterministic.

build(directory, count=16)  the 16 app databases (count 16), or those plus synthetic app
                            databases up to `count` (up to 40 and more): varied sizes and table
                            counts, some with WAL, some with dates.

NAMES16 lists the 16 display names in order. PHONES[i] is '+9198765%05d' % i; 'zebracorn'
appears once in msgstore.db and once in transactions_db.
"""

import os
import random
import shutil
import sqlite3

PHONES = ["+9198765%05d" % i for i in range(400)]
JIDS = ["9198765%05d@s.whatsapp.net" % i for i in range(400)]
T0_MS = 1672531200000               # 2023-01-01 00:00:00 UTC
T0_S = T0_MS // 1000
DAY_MS = 86400000
# Chrome's WebKit time: microseconds since 1601-01-01
WEBKIT_OFFSET_US = 11644473600 * 1000000

NAMES16 = ["msgstore.db", "wa.db", "axolotl.db", "accounts_db", "transactions_db",
           "notifications.db", "analytics.db", "cache.db", "kv_store.db", "contacts2.db",
           "calllog.db", "mmssms.db", "History", "Cookies", "calendar.db", "downloads.db"]


def _path(directory, rel):
    path = os.path.join(directory, *rel.split("/"))
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    for sfx in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + sfx):
            os.remove(path + sfx)
    return path


def _finish(path, c, wal_edit=None):
    """Commit and close; with wal_edit(c), leave a live WAL holding that edit (the files are
    copied while the writer is open: SQLite would checkpoint the WAL away on close)."""
    c.commit()
    if wal_edit is None:
        c.close()
        return path
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    wal_edit(c)
    c.commit()
    tmp = path + ".copy"
    shutil.copyfile(path, tmp)
    shutil.copyfile(path + "-wal", tmp + "-wal")
    c.close()
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(path + sfx):
            os.remove(path + sfx)
    os.rename(tmp, path)
    os.rename(tmp + "-wal", path + "-wal")
    return path


def msgstore(d, rnd):
    path = _path(d, "data/com.whatsapp/databases/msgstore.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, raw_string TEXT, type INTEGER);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT,
                          created_timestamp INTEGER);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                             key_remote_jid TEXT, text_data TEXT, timestamp INTEGER,
                             received_timestamp INTEGER);
        CREATE TABLE message_media(message_row_id INTEGER PRIMARY KEY, file_path TEXT,
                                   file_size INTEGER, mime_type TEXT);
        CREATE INDEX message_chat ON message(chat_row_id);
    """)
    c.executemany("INSERT INTO jid VALUES (?,?,0)", [(i + 1, JIDS[i]) for i in range(120)])
    c.executemany("INSERT INTO chat VALUES (?,?,?,?)",
                  [(i + 1, i + 1, "chat %d" % i, T0_MS + i * DAY_MS) for i in range(60)])
    rows = []
    for i in range(1, 3001):
        chat = 1 + rnd.randrange(60)
        when = T0_MS + i * 40 * 60000 + rnd.randrange(60000)
        text = "the zebracorn arrives" if i == 77 else "message %d %s" % (i, "x" * rnd.randrange(40))
        rows.append((i, chat, i % 2, JIDS[chat - 1], text, when, when + 1500))
    c.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO message_media VALUES (?,?,?,?)",
                  [(i, "Media/IMG-%05d.jpg" % i, 1000 + i * 17, "image/jpeg")
                   for i in range(1, 3001, 9)])
    return _finish(path, c, lambda c: c.execute(
        "UPDATE message SET text_data='edited in the WAL' WHERE _id=5"))


def wa(d, rnd):
    path = _path(d, "data/com.whatsapp/databases/wa.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE wa_contacts(_id INTEGER PRIMARY KEY, jid TEXT UNIQUE, "
              "display_name TEXT, number TEXT, status TEXT, last_seen INTEGER)")
    c.executemany("INSERT INTO wa_contacts VALUES (?,?,?,?,?,?)",
                  [(i + 1, JIDS[i], "Contact %d" % i, PHONES[i], "Hey there",
                    T0_S + i * 5400) for i in range(150)])
    return _finish(path, c)


def axolotl(d, rnd):
    path = _path(d, "data/com.whatsapp/databases/axolotl.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE identities(_id INTEGER PRIMARY KEY, recipient_id TEXT, public_key BLOB);
        CREATE TABLE prekeys(_id INTEGER PRIMARY KEY, prekey_id INTEGER, record BLOB);
    """)
    c.executemany("INSERT INTO identities VALUES (?,?,?)",
                  [(i + 1, JIDS[i], bytes(rnd.randrange(256) for _ in range(33)))
                   for i in range(80)])
    c.executemany("INSERT INTO prekeys VALUES (?,?,?)",
                  [(i + 1, i, bytes(rnd.randrange(256) for _ in range(64))) for i in range(200)])
    return _finish(path, c)


def accounts(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/accounts_db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE account(account_id TEXT PRIMARY KEY, phone TEXT, upi_id TEXT,
                             bank TEXT, created_at INTEGER);
        CREATE TABLE linked_bank(_id INTEGER PRIMARY KEY, account_id TEXT, ifsc TEXT,
                                 masked_number TEXT, added_on INTEGER);
    """)
    c.executemany("INSERT INTO account VALUES (?,?,?,?,?)",
                  [("ACC%06d" % i, PHONES[i], "user%d@ybl" % i, "Bank %d" % (i % 7),
                    T0_MS - 200 * DAY_MS + i * DAY_MS) for i in range(40)])
    c.executemany("INSERT INTO linked_bank VALUES (?,?,?,?,?)",
                  [(i + 1, "ACC%06d" % (i % 40), "IFSC%07d" % i, "XXXX%04d" % i,
                    T0_MS - 100 * DAY_MS + i * DAY_MS) for i in range(55)])
    return _finish(path, c)


def transactions(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/transactions_db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE txn(txn_id TEXT PRIMARY KEY, account_id TEXT, amount REAL,
                         counterparty_phone TEXT, note TEXT, status TEXT, created TEXT);
        CREATE TABLE txn_state(_id INTEGER PRIMARY KEY, txn_id TEXT, state TEXT,
                               updated_at INTEGER);
    """)
    rows, states = [], []
    import datetime
    base = datetime.datetime(2023, 1, 1)
    for i in range(6000):
        when = base + datetime.timedelta(minutes=17 * i)
        rows.append(("T%08d" % i, "ACC%06d" % (i % 40), round(rnd.random() * 5000, 2),
                     PHONES[rnd.randrange(300)], "zebracorn payment" if i == 4242 else
                     "payment %d" % i, "SUCCESS" if i % 11 else "FAILED",
                     when.strftime("%Y-%m-%dT%H:%M:%SZ")))
        states.append((i + 1, "T%08d" % i, "DONE", T0_MS + i * 17 * 60000 + 5000))
    c.executemany("INSERT INTO txn VALUES (?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO txn_state VALUES (?,?,?,?)", states)
    return _finish(path, c, lambda c: c.execute(
        "UPDATE txn SET status='REVERSED' WHERE txn_id='T00000010'"))


def notifications(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/notifications.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE notification(_id INTEGER PRIMARY KEY, title TEXT, body TEXT, "
              "received TEXT, read INTEGER)")
    import datetime
    base = datetime.datetime(2023, 2, 1)
    c.executemany("INSERT INTO notification VALUES (?,?,?,?,?)",
                  [(i + 1, "Offer %d" % i, "Cashback on your next payment %d" % i,
                    (base + datetime.timedelta(hours=7 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                    i % 2) for i in range(400)])
    return _finish(path, c)


def analytics(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/analytics.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE events(_id INTEGER PRIMARY KEY, name TEXT, payload TEXT, "
              "ts INTEGER)")
    c.executemany("INSERT INTO events VALUES (?,?,?,?)",
                  [(i + 1, "screen_%d" % (i % 30), "{\"k\": %d, \"pad\": \"%s\"}"
                    % (i, "p" * 60), T0_S + i * 97) for i in range(25000)])
    return _finish(path, c)


def cache(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/cache.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE response_cache(url TEXT PRIMARY KEY, body BLOB, expires INTEGER);
        CREATE TABLE image_cache(key TEXT PRIMARY KEY, path TEXT);
    """)
    return _finish(path, c)


def kv_store(d, rnd):
    path = _path(d, "data/com.phonepe.app/databases/kv_store.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT)")
    c.executemany("INSERT INTO kv VALUES (?,?)", [("pref_%d" % i, "v%d" % i) for i in range(30)])
    _finish(path, c)
    # an interrupted transaction: a rollback journal with a valid header (a warning)
    with open(path + "-journal", "wb") as f:
        f.write(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 504)
    return path


def contacts2(d, rnd):
    path = _path(d, "data/com.android.providers.contacts/databases/contacts2.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE raw_contacts(_id INTEGER PRIMARY KEY, display_name TEXT,
                                  last_time_contacted INTEGER, times_contacted INTEGER);
        CREATE TABLE data(_id INTEGER PRIMARY KEY, raw_contact_id INTEGER, mimetype TEXT,
                          data1 TEXT);
        CREATE TABLE phone_lookup(data_id INTEGER, raw_contact_id INTEGER,
                                  normalized_number TEXT);
    """)
    c.executemany("INSERT INTO raw_contacts VALUES (?,?,?,?)",
                  [(i + 1, "Contact %d" % i, T0_MS + i * 3 * DAY_MS, i % 9) for i in range(180)])
    c.executemany("INSERT INTO data VALUES (?,?,?,?)",
                  [(i + 1, i + 1, "vnd.android.cursor.item/phone_v2", PHONES[i])
                   for i in range(180)])
    c.executemany("INSERT INTO phone_lookup VALUES (?,?,?)",
                  [(i + 1, i + 1, PHONES[i]) for i in range(180)])
    return _finish(path, c)


def calllog(d, rnd):
    path = _path(d, "data/com.android.providers.contacts/databases/calllog.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE calls(_id INTEGER PRIMARY KEY, number TEXT, date INTEGER, "
              "duration INTEGER, type INTEGER)")
    c.executemany("INSERT INTO calls VALUES (?,?,?,?,?)",
                  [(i + 1, PHONES[rnd.randrange(200)], T0_MS + i * 3 * 3600000,
                    rnd.randrange(600), 1 + i % 3) for i in range(900)])
    return _finish(path, c)


def mmssms(d, rnd):
    path = _path(d, "data/com.android.providers.telephony/databases/mmssms.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE threads(_id INTEGER PRIMARY KEY, date INTEGER, recipient_ids TEXT);
        CREATE TABLE sms(_id INTEGER PRIMARY KEY, thread_id INTEGER, address TEXT,
                         date INTEGER, date_sent INTEGER, body TEXT, type INTEGER);
    """)
    c.executemany("INSERT INTO threads VALUES (?,?,?)",
                  [(i + 1, T0_MS + i * DAY_MS, str(i + 1)) for i in range(50)])
    c.executemany("INSERT INTO sms VALUES (?,?,?,?,?,?,?)",
                  [(i + 1, 1 + i % 50, PHONES[i % 50], T0_MS + i * 2 * 3600000,
                    T0_MS + i * 2 * 3600000 - 3000, "Your OTP is %06d" % rnd.randrange(10 ** 6),
                    1 + i % 2) for i in range(1500)])
    return _finish(path, c, lambda c: c.execute("DELETE FROM sms WHERE _id=3"))


def history(d, rnd):
    path = _path(d, "data/com.android.chrome/app_chrome/Default/History")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER,
                          last_visit_time INTEGER);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,
                            transition INTEGER);
        CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT);
    """)
    base = T0_S * 1000000 + WEBKIT_OFFSET_US
    c.executemany("INSERT INTO urls VALUES (?,?,?,?,?)",
                  [(i + 1, "https://example.com/page/%d" % i, "Page %d" % i, 1 + i % 5,
                    base + i * 3600 * 1000000) for i in range(700)])
    c.executemany("INSERT INTO visits VALUES (?,?,?,?)",
                  [(i + 1, 1 + i % 700, base + i * 1800 * 1000000, 805306368)
                   for i in range(1400)])
    c.executemany("INSERT INTO keyword_search_terms VALUES (?,?,?)",
                  [(1, i + 1, "search %d" % i) for i in range(60)])
    return _finish(path, c)


def cookies(d, rnd):
    path = _path(d, "data/com.android.chrome/app_chrome/Default/Cookies")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE cookies(creation_utc INTEGER, host_key TEXT, name TEXT, "
              "value TEXT, expires_utc INTEGER, last_access_utc INTEGER)")
    base = T0_S * 1000000 + WEBKIT_OFFSET_US
    c.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?)",
                  [(base + i * 7200 * 1000000, ".site%d.example" % (i % 40), "sid", "v%d" % i,
                    base + (i + 9000) * 7200 * 1000000, base + (i + 5) * 7200 * 1000000)
                   for i in range(320)])
    return _finish(path, c)


def calendar(d, rnd):
    path = _path(d, "data/com.google.android.calendar/databases/calendar.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE Calendars(_id INTEGER PRIMARY KEY, name TEXT, account_name TEXT);
        CREATE TABLE Events(_id INTEGER PRIMARY KEY, calendar_id INTEGER, title TEXT,
                            dtstart INTEGER, dtend INTEGER);
    """)
    c.executemany("INSERT INTO Calendars VALUES (?,?,?)",
                  [(i + 1, "Calendar %d" % i, "user%d@example.com" % i) for i in range(3)])
    c.executemany("INSERT INTO Events VALUES (?,?,?,?,?)",
                  [(i + 1, 1 + i % 3, "Meeting %d" % i, T0_MS + i * DAY_MS,
                    T0_MS + i * DAY_MS + 3600000) for i in range(240)])
    return _finish(path, c)


def downloads(d, rnd):
    path = _path(d, "data/com.android.providers.downloads/databases/downloads.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE downloads(_id INTEGER PRIMARY KEY, uri TEXT, _data TEXT, "
              "mimetype TEXT, total_bytes INTEGER, lastmod INTEGER)")
    c.executemany("INSERT INTO downloads VALUES (?,?,?,?,?,?)",
                  [(i + 1, "https://files.example.com/f%d.pdf" % i,
                    "/sdcard/Download/f%d.pdf" % i, "application/pdf", 1000 * i,
                    T0_MS + i * 5 * DAY_MS) for i in range(45)])
    return _finish(path, c)


BUILDERS16 = [msgstore, wa, axolotl, accounts, transactions, notifications, analytics, cache,
              kv_store, contacts2, calllog, mmssms, history, cookies, calendar, downloads]


def synthetic(d, rnd, i):
    """A made-up app database: 1..60 tables, 0..4000 rows each, some with dates, every third
    one with a live WAL."""
    path = _path(d, "data/com.example.app%02d/databases/app%02d.db" % (i, i))
    c = sqlite3.connect(path)
    ntables = rnd.choice((1, 2, 3, 5, 8, 12, 25, 60))
    for t in range(ntables):
        name = "%s_%d" % (rnd.choice(("items", "log", "sync", "users", "state", "blob")), t)
        dated = rnd.random() < 0.5
        c.execute("CREATE TABLE %s(_id INTEGER PRIMARY KEY, label TEXT, ref TEXT%s)"
                  % (name, ", updated_at INTEGER" if dated else ""))
        n = rnd.choice((0, 0, 3, 40, 300, 4000)) if ntables < 20 else rnd.choice((0, 5, 20))
        if dated:
            c.executemany("INSERT INTO %s VALUES (?,?,?,?)" % name,
                          [(k + 1, "label %d" % k, PHONES[k % 400], T0_MS + k * 3600000)
                           for k in range(n)])
        else:
            c.executemany("INSERT INTO %s VALUES (?,?,?)" % name,
                          [(k + 1, "label %d" % k, "ref%d" % k) for k in range(n)])
    if i % 3 == 0:
        c.execute("CREATE TABLE IF NOT EXISTS wal_edits(_id INTEGER PRIMARY KEY, note TEXT)")
        return _finish(path, c, lambda c: c.execute(
            "INSERT INTO wal_edits(note) VALUES ('written in the WAL')"))
    return _finish(path, c)


def build(directory, count=16, seed=1234):
    """The paths of `count` databases in directory (the 16 app databases first)."""
    rnd = random.Random(seed)
    paths = []
    for fn in BUILDERS16[:count]:
        paths.append(fn(directory, rnd))
    for i in range(count - len(paths)):
        paths.append(synthetic(directory, rnd, i + 1))
    return paths
