"""A case of three databases, as one app keeps them side by side: messages (chats, their
contacts as jid text), contacts (the same jids with names) and a settings database that
shares nothing with the others. Built at test time into a caller-supplied directory.

  messages.db   jid(_id, raw_string), chat(_id, jid_row_id, subject), message(_id,
                chat_row_id, key_remote_jid, text_data, timestamp ms) - in WAL mode, with a
                live WAL (an edited message)
  contacts.db   wa_contacts(_id, jid UNIQUE, display_name, number, last_seen s) - 15 of the
                20 jids, and 5 of its own
  settings.db   prefs(key, value) - nothing in common with the others

JIDS[i] is '<10000 + i>@s.example.net'. A distinctive word, 'zebracorn', is in one message
and one contact name; 'nothing-here' is nowhere.
"""

import os
import shutil
import sqlite3

JIDS = ["%d@s.example.net" % (10000 + i) for i in range(20)]
CONTACT_JIDS = JIDS[:15] + ["%d@s.example.net" % (90000 + i) for i in range(5)]
T0_MS = 1614834367000           # 2021-03-04 05:06:07 UTC
T0_S = T0_MS // 1000


def _fresh(directory, name):
    path = os.path.join(directory, name)
    for sfx in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + sfx):
            os.remove(path + sfx)
    return path, sqlite3.connect(path)


def messages(directory, name="messages.db", wal=True):
    path, c = _fresh(directory, name)
    c.executescript("""
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, raw_string TEXT);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INTEGER,
                             key_remote_jid TEXT, text_data TEXT, timestamp INTEGER);
        CREATE INDEX message_chat ON message(chat_row_id);
    """)
    c.executemany("INSERT INTO jid VALUES (?,?)", [(i + 1, j) for i, j in enumerate(JIDS)])
    c.executemany("INSERT INTO chat VALUES (?,?,?)",
                  [(i + 1, i + 1, "chat with %s" % JIDS[i].split("@")[0]) for i in range(10)])
    rows = []
    for i in range(1, 121):
        chat = 1 + i % 10
        text = "the zebracorn message" if i == 7 else "message %d" % i
        rows.append((i, chat, JIDS[chat - 1], text, T0_MS + i * 60000))
    c.executemany("INSERT INTO message VALUES (?,?,?,?,?)", rows)
    c.commit()
    if not wal:
        c.close()
        return path
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("UPDATE message SET text_data='edited message 5' WHERE _id=5")
    c.commit()
    # copy the files while the writer is open: SQLite checkpoints the WAL away on close
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


def contacts(directory, name="contacts.db"):
    path, c = _fresh(directory, name)
    c.executescript("""
        CREATE TABLE wa_contacts(_id INTEGER PRIMARY KEY, jid TEXT UNIQUE, display_name TEXT,
                                 number TEXT, last_seen INTEGER);
    """)
    c.executemany("INSERT INTO wa_contacts VALUES (?,?,?,?,?)",
                  [(i + 1, j, "Zebracorn Person" if i == 3 else "Person %d" % i,
                    "+1555%07d" % i, T0_S + i * 3600) for i, j in enumerate(CONTACT_JIDS)])
    c.commit()
    c.close()
    return path


def settings(directory, name="settings.db"):
    path, c = _fresh(directory, name)
    c.executescript("CREATE TABLE prefs(key TEXT PRIMARY KEY, value TEXT);")
    c.executemany("INSERT INTO prefs VALUES (?,?)",
                  [("pref_%d" % i, "value_%d" % i) for i in range(12)])
    c.commit()
    c.close()
    return path


def build(directory):
    """(messages, contacts, settings) paths in directory."""
    return messages(directory), contacts(directory), settings(directory)
