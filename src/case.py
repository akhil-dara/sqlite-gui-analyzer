"""The databases open together (a case) and which one is active.

Case holds a Member per database: its own database.DB (an engine Session: read-only,
immutable, its own WAL overlay and evidence hashes), a short unique name, a stable colour,
its row counts and its search scope. One member is active: App.db is the active member's DB,
so every tab written for one database works on it unchanged. A case of one database is simply
the database that was opened.

No Tk here.
"""

import os
import threading

from database import DB
from engine.case import chip_colour, db_identity, display_names
from engine.evidence import clear_guard_cache, inside_any, linked_file
from engine.tags import valid_color
from constants import mode_label


class Member(object):
    """One database of a case."""

    def __init__(self, uid, db, color):
        self.uid, self.db, self.color = uid, db, color
        self._path = db.evidence.main       # kept: a closed database has no evidence set
        self.name = os.path.basename(self._path)
        self.counts = {}            # table -> row count ('?' / '~n' while counting)
        self.scope_tables = []      # tables (and views) the search covers
        self.scope_views_seen = None
        self.load_time = 0.0
        self.saved = None           # this database as the reopened case file recorded it
        self.changes = []           # how it differs from that record ('size ...', 'missing')

    @property
    def path(self):
        """The database's path (also once it has left the case and is closed)."""
        return self._path

    @property
    def size(self):
        fp = self.db.evidence.fingerprints.get("main") if self.db.ok else None
        return fp.size if fp is not None else None

    @property
    def identity(self):
        return db_identity(self.path, self.size)

    def sha256(self):
        fp = self.db.evidence.fingerprints.get("main") if self.db.ok else None
        return fp.sha256 if fp is not None else None

    def status(self):
        """Short status for the case bar: how the database was opened."""
        db = self.db
        if not db.ok:
            return "closed"
        parts = [mode_label(db.mode)]
        try:
            if db.evidence.journal_is_hot():
                parts.append("hot journal")
        except OSError:
            pass
        return ", ".join(parts)

    def label(self, table=None):
        """'wa.db' or 'wa.db › table'."""
        return self.name if table is None else "%s › %s" % (self.name, table)

    def __repr__(self):
        return "Member(%r)" % self.name


class Case(object):
    """The open databases (members, in the order they were added) and the active one."""

    def __init__(self):
        self.members = []
        self.active = None
        self._next = 1
        self._lock = threading.Lock()
        self.extra_folders = []     # protect_folder(): more folders the write guard refuses

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(list(self.members))

    @property
    def multi(self):
        """True when several databases are open (the case bar and database labels show)."""
        return len(self.members) > 1

    def find(self, uid):
        for m in self.members:
            if m.uid == uid:
                return m
        return None

    def by_path(self, path):
        # Compare by real path too: the same file opened through a symlink (or another
        # spelling) must not join the case twice.
        norms = {os.path.normcase(os.path.abspath(path))}
        try:
            norms.add(os.path.normcase(os.path.realpath(path)))
        except OSError:
            pass
        for m in self.members:
            mine = {os.path.normcase(os.path.abspath(m.path))}
            try:
                mine.add(os.path.normcase(os.path.realpath(m.path)))
            except OSError:
                pass
            if norms & mine:
                return m
        return None

    def of_db(self, db):
        for m in self.members:
            if m.db is db:
                return m
        return None

    def add(self, path, color=None, ram_limit=None):
        """Open a database into the case (the first becomes the active one): returns its
        Member. The file is opened like a single database; errors propagate."""
        db = DB()
        db.open(path, ram_limit)
        return self.add_db(db, color)

    def add_db(self, db, color=None):
        """A database already opened (on a worker thread) joins the case: returns its
        Member."""
        with self._lock:
            used = [m.color for m in self.members]
            color = valid_color(color, None)        # '#rrggbb' only: it ends up in HTML / SVG
            if not color or color.lower() in (c.lower() for c in used):
                color = chip_colour(used)
            m = Member(self._next, db, color)
            self._next += 1
            self.members.append(m)
            if self.active is None:
                self.active = m
        clear_guard_cache()             # the protected folders changed
        self._rename()
        return m

    def remove(self, member, report=None):
        """Close one database and take it out of the case; returns its evidence report (the
        one given, when the caller verified the evidence already). The next member becomes
        active when it was the active one."""
        if member not in self.members:
            return None
        with self._lock:
            i = self.members.index(member)
            self.members.remove(member)
            if self.active is member:
                self.active = self.members[min(i, len(self.members) - 1)] \
                    if self.members else None
        clear_guard_cache()
        self._rename()
        return member.db.close(report)

    def set_active(self, member):
        if member in self.members:
            self.active = member

    def _rename(self):
        names = display_names([m.path for m in self.members])
        for m, n in zip(self.members, names):
            m.name = n

    def interrupt(self, thread=None):
        """Cancel running SQL in every database (on one worker thread, or all)."""
        for m in list(self.members):
            if m.db.ok:
                m.db.interrupt(thread)

    def release_thread_connection(self):
        """Close this worker thread's connection to every database of the case."""
        for m in list(self.members):
            s = m.db.session
            if s is not None:
                s.release_thread_connection()

    def protect_folder(self, folder):
        """Also refuse writes inside folder (the folder a case was opened from, the folders of
        the databases found there, a database that failed to open) until a new case."""
        if folder:
            with self._lock:
                if folder not in self.extra_folders:
                    self.extra_folders.append(folder)
            clear_guard_cache()

    def clear_protected_folders(self):
        """A new case: forget the extra folders of the previous one."""
        with self._lock:
            del self.extra_folders[:]
        clear_guard_cache()

    def protected_folders(self):
        """Every folder writes are refused in: each database's (also one closed or not open),
        and the extra ones (protect_folder)."""
        out, seen = [], set()
        for f in [os.path.dirname(os.path.abspath(m.path)) for m in list(self.members)] + \
                list(self.extra_folders):
            k = os.path.normcase(os.path.abspath(f))
            if k not in seen:
                seen.add(k)
                out.append(f)
        return out

    def is_protected(self, path):
        """True when writing to path would touch the evidence: it lies in a protected folder
        (protected_folders, compared under every name: engine.evidence.inside_any), or it
        is an existing file with several names (a hard link) or another name of an evidence
        file (engine.evidence.linked_file)."""
        if not path:
            return False
        if inside_any(path, self.protected_folders()) is not None:
            return True
        ids = []
        for m in list(self.members):
            if m.db.ok:
                ids.extend(m.db.evidence.identities())
        return linked_file(path, ids)

    def evidence_dirs(self):
        """The evidence folders of the case, each once (many databases share a folder): the
        same folders the write guard refuses (protected_folders)."""
        return self.protected_folders()
