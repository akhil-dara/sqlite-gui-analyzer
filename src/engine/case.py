"""Several databases examined together: finding them in a folder and naming them.

A case is a list of evidence databases opened side by side (each one its own read-only
Session, see session.py). This module holds what needs no Tk and no open database:

  scan_folder()     the SQLite databases in a folder (optionally its subfolders): only files
                    that start with the SQLite header, with size, WAL / journal presence and
                    the path relative to the folder. Reads 16 bytes of each file, read-only.
  db_identity()     how a database is recognised across sessions: normalised path + size.
  display_names()   short names for the databases of a case, unique within it.
  chip_colour()     a stable colour per database of a case.

Case files (the list of databases, saved in the app-data folder) are written and read by
engine.tags (write_case / read_case), next to the tag files.
"""

import os

from .fileformat.header import MAGIC
from .limits import get

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# chip colours: distinct at a glance, readable as text on white
PALETTE = ("#0065ff", "#d9480f", "#2b8a3e", "#862e9c", "#c2255c", "#0b7285", "#5c940d",
           "#e67700", "#364fc7", "#a61e4d", "#087f5b", "#495057")


class Candidate(object):
    """A file of a scanned folder that starts with the SQLite header."""
    __slots__ = ("path", "rel", "size", "wal", "wal_size", "journal")

    def __init__(self, path, rel, size, wal_size=None, journal=False):
        self.path, self.rel, self.size = path, rel, size
        self.wal = wal_size is not None
        self.wal_size = wal_size or 0
        self.journal = journal

    @property
    def name(self):
        return os.path.basename(self.path)

    def sidecars_text(self):
        parts = []
        if self.wal:
            parts.append("WAL (%s bytes)" % format(self.wal_size, ","))
        if self.journal:
            parts.append("journal")
        return ", ".join(parts)

    def __repr__(self):
        return "Candidate(%r, %d)" % (self.rel, self.size)


def is_sqlite_file(path):
    """True when the file starts with 'SQLite format 3\\0' (16 bytes read, read-only)."""
    try:
        with open(path, "rb") as f:
            head = f.read(101)
    except OSError:
        return False
    if head[:len(MAGIC)] == MAGIC:
        return True
    # only the signature damaged, the rest of the header and page 1 as SQLite writes them
    from .fileformat.header import signature_only_damaged
    return len(head) == 101 and signature_only_damaged(head)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def candidate(path, folder=None):
    """A Candidate for one file (None when it is not a SQLite database)."""
    if not is_sqlite_file(path):
        return None
    size = _size(path)
    rel = os.path.relpath(path, folder) if folder else os.path.basename(path)
    return Candidate(os.path.abspath(path), rel, size or 0, _size(path + "-wal"),
                     os.path.isfile(path + "-journal"))


def scan_folder(folder, recursive=False, cancel=None, progress=None, limit=None, accept=None,
                stats=None):
    """(candidates, files looked at, problems) for the SQLite databases in folder.

    Sidecar files (-wal, -shm, -journal) are not listed on their own: they are shown with their
    database. cancel() true stops (the files found so far are returned); progress(n) is
    called with the number of files looked at; at most `limit` files (default: the
    folder_scan_files limit), and the problems say when that stopped the scan. Nothing is
    written anywhere.

    accept(name, relative path, size()) -> False leaves a file out before it is opened (the
    user's conditions, engine.filematch: a scan of a whole users folder for the files named
    one way reads only those); such files do not count against the limit. stats, a dict,
    receives 'walked' (files seen in the folders), 'skipped' (left out by accept), 'found' and
    'candidates' (the databases found so far, kept up to date while the scan runs)."""
    if limit is None:
        limit = get("folder_scan_files")
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return [], 0, ["%s: not a folder (or not readable)" % folder]
    found, problems = [], []
    seen = [0]

    unreadable = []

    walked, skipped = [0], [0]
    if stats is not None:
        stats["candidates"] = found     # the databases found so far (read by the UI)

    def look(path):
        if path.lower().endswith(SIDECAR_SUFFIXES):
            return
        walked[0] += 1
        if stats is not None:
            stats["walked"] = walked[0]     # (read by the scan's progress line)
        if accept is not None:
            rel = os.path.relpath(path, folder)
            if not accept(os.path.basename(path), rel, lambda: _size(path)):
                skipped[0] += 1
                if progress is not None and walked[0] % 500 == 0:
                    progress(seen[0])
                return
        seen[0] += 1
        if progress is not None and seen[0] % 50 == 0:
            progress(seen[0])
        try:
            with open(path, "rb") as f:
                head = f.read(len(MAGIC))
        except OSError as e:            # locked, no permission, gone meanwhile
            unreadable.append("%s (%s)" % (os.path.relpath(path, folder), e.strerror or e))
            return
        if head != MAGIC:
            return                      # not a SQLite database (or an encrypted one)
        c = candidate(path, folder)
        if c is not None:
            found.append(c)
            if stats is not None:
                stats["found"] = len(found)

    def stop():
        return (cancel is not None and cancel()) or seen[0] >= limit

    if recursive:
        def onerror(e):
            problems.append("%s: %s" % (getattr(e, "filename", "") or folder, e.strerror or e))
        for root, dirs, files in os.walk(folder, onerror=onerror):
            dirs.sort()
            for name in sorted(files):
                if stop():
                    break
                look(os.path.join(root, name))
            if stop():
                break
    else:
        try:
            names = sorted(os.listdir(folder))
        except OSError as e:
            return [], 0, ["%s: %s" % (folder, e.strerror or e)]
        for name in names:
            if stop():
                break
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                look(p)
    if seen[0] >= limit:
        problems.append("stopped after %s files (limit folder_scan_files)" % format(limit, ","))
    if unreadable:
        problems.append("%d file%s could not be read: %s" % (
            len(unreadable), "" if len(unreadable) == 1 else "s",
            ", ".join(unreadable[:5]) + (" …" if len(unreadable) > 5 else "")))
    if progress is not None:
        progress(seen[0])
    if stats is not None:
        stats["walked"], stats["skipped"] = walked[0], skipped[0]
    found.sort(key=lambda c: c.rel.lower())
    return found, seen[0], problems


def norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def db_identity(path, size):
    """A database's identity across sessions: its normalised absolute path and its size."""
    return "%s|%s" % (norm_path(path), "?" if size is None else int(size))


BROWSERS = (("google/chrome", "Chrome"), ("chromium", "Chromium"), ("microsoft/edge", "Edge"),
            ("bravesoftware", "Brave"), ("opera software", "Opera"), ("vivaldi", "Vivaldi"),
            ("mozilla/firefox", "Firefox"))
PREFERENCES_MAX = 4 << 20       # a Chromium profile's Preferences file read for its name


def browser_profile(path):
    """('Chrome', 'Alex') for a database inside a browser profile folder: the browser from
    the path, the profile's own name from its Preferences file (Chromium; read-only, at most
    PREFERENCES_MAX bytes) when there is one; (None, None) otherwise."""
    norm = os.path.abspath(path).replace("\\", "/").lower()
    browser = next((name for key, name in BROWSERS if key in norm), None)
    if browser is None:
        return None, None
    prefs = os.path.join(os.path.dirname(os.path.abspath(path)), "Preferences")
    try:
        if os.path.getsize(prefs) > PREFERENCES_MAX:
            return browser, None
        import json
        with open(prefs, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        name = (data.get("profile") or {}).get("name")
        return browser, (str(name)[:40] if name else None)
    except (OSError, ValueError, AttributeError):
        return browser, None


def _differing_folder(paths, i):
    """The nearest folder (from the file upwards) where paths[i] differs from every other
    path of the group: 'Profile 2' for Chrome profiles, 'phone_a' for two copies of
    com.whatsapp/databases/msgstore.db."""
    split = [os.path.dirname(os.path.abspath(p)).replace("\\", "/").rstrip("/").split("/")
             for p in paths]
    mine = split[i]
    others = [o for j, o in enumerate(split) if j != i]
    for depth in range(1, len(mine) + 1):
        seg = mine[-depth]
        if all(len(o) < depth or o[-depth].lower() != seg.lower() for o in others):
            # the folders from there down to where every path agrees again (Android keeps
            # one app under data/data, user/0 and data_mirror/.../0: 'user/0', not 'user')
            low = depth - 1
            while low > 0 and not all(len(o) >= low and o[-low].lower() == mine[-low].lower()
                                      for o in others):
                low -= 1
            return _readable_segment(mine, depth, low)
    # no single folder tells this path from every other one (several users each with a
    # 'Default' profile, say): the shortest run of folders above the shared tail that does
    tail = 0
    while all(len(o) > tail and o[-tail - 1].lower() == mine[-tail - 1].lower()
              for o in others) and tail < len(mine) - 1:
        tail += 1
    for depth in range(tail + 1, len(mine) + 1):
        run = [x.lower() for x in mine[-depth:len(mine) - tail]]
        if all([x.lower() for x in o[-depth:len(o) - tail]] != run for o in others):
            return _readable_segment(mine, depth, tail)
    return mine[-1] if mine else "?"


def _readable_segment(parts, depth, low=0):
    """The folders parts[-depth] .. parts[-low-1], said plainly: widened upwards while the top
    one says nothing on its own (a bare number, 'null'), a long id shortened in the middle
    ('1007080…9753')."""
    top = depth
    while top < len(parts) and (parts[-top].isdigit() and len(parts[-top]) <= 4
                                or parts[-top].lower() == "null"):
        top += 1
    segs = parts[-top:len(parts) - low]
    out = []
    for seg in segs:
        if len(seg) >= 16 and seg.replace("-", "").isalnum() and any(ch.isdigit() for ch in seg):
            seg = "%s…%s" % (seg[:7], seg[-4:])
        out.append(seg)
    return "/".join(out)


def display_names(paths, profiles=True):
    """Short unique names for these database paths, in order: the file name; for equal file
    names, the file name and the nearest folder that tells them apart ('History (Profile 2)',
    'msgstore.db (phone_a)'), with the browser and the profile's own name for browser
    profiles ('History (Chrome Profile 2 · Alex)'); a number when even that repeats."""
    base = [os.path.basename(p) or p for p in paths]
    out = list(base)
    lower = [b.lower() for b in base]
    groups = {}
    for i, k in enumerate(lower):
        groups.setdefault(k, []).append(i)
    for idx in groups.values():
        if len(idx) < 2:
            continue
        group = [paths[i] for i in idx]
        for n, i in enumerate(idx):
            where = _differing_folder(group, n)
            browser, person = browser_profile(paths[i]) if profiles else (None, None)
            if browser:
                where = "%s %s" % (browser, where) + (" \u00b7 %s" % person if person else "")
            out[i] = "%s (%s)" % (base[i], where)
    seen = {}
    for i, name in enumerate(out):
        k = name.lower()
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            out[i] = "%s #%d" % (name, seen[k])
    return out


def chip_colour(used):
    """The first palette colour not in `used` (a colour per database of a case)."""
    taken = set(c.lower() for c in used if c)
    for c in PALETTE:
        if c.lower() not in taken:
            return c
    return PALETTE[len(taken) % len(PALETTE)]
