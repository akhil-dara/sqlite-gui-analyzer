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
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


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


def scan_folder(folder, recursive=False, cancel=None, progress=None, limit=None):
    """(candidates, files looked at, problems) for the SQLite databases in folder.

    Sidecar files (-wal, -shm, -journal) are not listed on their own: they are shown with their
    database. cancel() true stops (the files found so far are returned); progress(n) is
    called with the number of files looked at; at most `limit` files (default: the
    folder_scan_files limit), and the problems say when that stopped the scan. Nothing is
    written anywhere."""
    if limit is None:
        limit = get("folder_scan_files")
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return [], 0, ["%s: not a folder (or not readable)" % folder]
    found, problems = [], []
    seen = [0]

    unreadable = []

    def look(path):
        if path.lower().endswith(SIDECAR_SUFFIXES):
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
    found.sort(key=lambda c: c.rel.lower())
    return found, seen[0], problems


def norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def db_identity(path, size):
    """A database's identity across sessions: its normalised absolute path and its size."""
    return "%s|%s" % (norm_path(path), "?" if size is None else int(size))


def display_names(paths):
    """Short unique names for these database paths, in order: the file name; for equal file
    names, the file name and its folder ('msgstore.db (backup)'); a number when even that
    repeats."""
    base = [os.path.basename(p) or p for p in paths]
    out = list(base)
    lower = [b.lower() for b in base]
    for i, p in enumerate(paths):
        if lower.count(lower[i]) > 1:
            parent = os.path.basename(os.path.dirname(os.path.abspath(p))) or "?"
            out[i] = "%s (%s)" % (base[i], parent)
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
