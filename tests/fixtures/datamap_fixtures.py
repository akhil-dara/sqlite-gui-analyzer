"""Fixture for "Copy with related" and the Database Map: a small browser-history-like database.

urls        id INTEGER PRIMARY KEY, title with characters Markdown and HTML must escape,
            last_visit_time in WebKit microseconds
visits      url -> urls.id (a number column named after the table: a name link, values all
            found), visit_time in WebKit microseconds
visit_source id REFERENCES visits(id) (declared, every third visit)
favicons    url_id REFERENCES urls(id) (declared), a binary property list BLOB, created in
            Cocoa seconds
keyword_search_terms  url_id -> urls.id by name, no INTEGER PRIMARY KEY (reached by rowid)
"odd <table> & name"  a table name that must be escaped
settings    message_id: named like a key but its values are nowhere (a weaker link)

Built at test time into a caller-supplied directory; the path of the file is returned.
"""

import os
import plistlib
import sqlite3
from datetime import datetime, timedelta

T0 = datetime(2021, 6, 1, 8, 30, 0)
URLS, VISITS = 20, 60
ODD = "odd <table> & name"


def webkit(dt):
    return (dt - datetime(1601, 1, 1)) // timedelta(microseconds=1)


def cocoa(dt):
    return (dt - datetime(2001, 1, 1)).total_seconds()


def visit_when(i):
    return T0 + timedelta(hours=5 * i, minutes=7 * i, seconds=i)


def browser(directory, name="browser.db"):
    path = os.path.join(directory, name)
    for sfx in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + sfx):
            os.remove(path + sfx)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,
                          visit_count INTEGER DEFAULT 0 NOT NULL,
                          last_visit_time INTEGER NOT NULL);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL,
                            visit_time INTEGER NOT NULL, transition INTEGER DEFAULT 0);
        CREATE INDEX visits_url_index ON visits(url);
        CREATE TABLE visit_source(id INTEGER PRIMARY KEY REFERENCES visits(id),
                                  source INTEGER NOT NULL);
        CREATE TABLE favicons(id INTEGER PRIMARY KEY, url_id INTEGER REFERENCES urls(id),
                              image BLOB, created REAL);
        CREATE TABLE keyword_search_terms(keyword_id INTEGER NOT NULL, url_id INTEGER NOT NULL,
                                          term LONGVARCHAR NOT NULL);
        CREATE TABLE settings(_id INTEGER PRIMARY KEY, message_id INTEGER, label TEXT);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, body TEXT);
    """)
    c.execute('CREATE TABLE "%s"(id INTEGER PRIMARY KEY, note TEXT)' % ODD.replace('"', '""'))
    for i in range(1, URLS + 1):
        last = max(visit_when(v) for v in range(VISITS) if 1 + v % URLS == i)
        c.execute("INSERT INTO urls VALUES (?,?,?,?,?)",
                  (i, "https://example.org/page/%d?a=1&b=2" % i, "Page | <b>%d</b>" % i,
                   VISITS // URLS, webkit(last)))
    for v in range(VISITS):
        c.execute("INSERT INTO visits VALUES (?,?,?,?)",
                  (v + 1, 1 + v % URLS, webkit(visit_when(v)), 805306368 + v % 3))
    for v in range(1, VISITS + 1, 3):
        c.execute("INSERT INTO visit_source VALUES (?,?)", (v, v % 4))
    for i in range(1, 11):
        blob = plistlib.dumps({"url": "https://example.org/page/%d" % i, "size": i * 16},
                              fmt=plistlib.FMT_BINARY)
        c.execute("INSERT INTO favicons(url_id, image, created) VALUES (?,?,?)",
                  (i, blob, cocoa(T0 + timedelta(days=i, minutes=13 * i))))
    for k in range(10):
        c.execute("INSERT INTO keyword_search_terms VALUES (?,?,?)",
                  (1 + k % 2, 1 + (k * 3) % URLS, "term %d" % k))
    c.executemany("INSERT INTO message(body) VALUES (?)", [("m %d" % i,) for i in range(5)])
    c.executemany("INSERT INTO settings(message_id, label) VALUES (?,?)",
                  [(900000 + i, "setting %d" % i) for i in range(30)])
    c.executemany('INSERT INTO "%s"(note) VALUES (?)' % ODD.replace('"', '""'),
                  [("<script>alert(%d)</script> | pipe" % i,) for i in range(3)])
    c.commit()
    c.close()
    return path
