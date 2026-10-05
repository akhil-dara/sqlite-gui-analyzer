"""Evidence handling: discover sidecars, fingerprint originals, verify, guard writes.

Nothing here (or anywhere in the engine) writes to the evidence directory.
"""

import hashlib
import os
import re
import threading

from .locks import read_range

try:
    from urllib.parse import quote
except ImportError:  # pragma: no cover
    from urllib import quote

SIDECAR_SUFFIXES = (("wal", "-wal"), ("shm", "-shm"), ("journal", "-journal"))
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
WAL_MAGICS = (bytes.fromhex("377f0682"), bytes.fromhex("377f0683"))
SQLITE_MAGIC = b"SQLite format 3\x00"
CHUNK = 1 << 20
OTHER_FILES_CAP = 200          # other_files() lists at most this many (other_files_more: the rest)

_slots = []
_slots_lock = threading.Lock()


def _hash_slots():
    """The semaphore letting 'hash_parallel' (engine.limits) evidence sets hash at once."""
    with _slots_lock:
        if not _slots:
            from .limits import get
            _slots.append(threading.BoundedSemaphore(get("hash_parallel")))
        return _slots[0]


def sqlite_uri(path, immutable=True):
    """Read-only SQLite URI for a filesystem path, safe for '#', '%', '?', spaces and UNC paths."""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p                       # C:/x -> /C:/x
    uri = "file://" + quote(p, safe="/:")  # -> file:///C:/x, file:////server/share/x
    return uri + "?mode=ro" + ("&immutable=1" if immutable else "")


_DRIVE_REMOTE = 4           # GetDriveTypeW: a mapped network drive


def _drive_is_remote(letter):
    """True when drive letter (e.g. 'Z') is a mapped network drive (Windows only; asks
    Windows for the drive's type, which does not connect to the server)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        return get_type("%s:\\" % letter) == _DRIVE_REMOTE
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def is_network_path(path):
    """True when opening or even checking path (os.stat, isfile, open) could make the
    computer connect to another one, which on Windows sends the user's credentials (NTLM) to
    it: a UNC path (\\\\server\\share, //server/share), the long forms \\\\?\\UNC\\ and
    \\\\.\\UNC\\, any other \\\\?\\ or \\\\.\\ device path that is not a plain drive letter,
    and a drive letter mapped to a network share. Decided from the text and the drive's
    type alone: path itself is never touched. A relative path is taken from the current
    folder."""
    if not isinstance(path, str) or not path:
        return False
    p = path.replace("/", "\\")
    if not (p.startswith("\\\\") or p.startswith("\\??\\")):
        if len(p) < 2 or p[1] != ":":
            try:
                p = os.path.abspath(path).replace("/", "\\")   # text only: the current folder
            except (OSError, ValueError):
                return True
    up = p.upper()
    for prefix in ("\\\\?\\", "\\\\.\\", "\\??\\"):
        if up.startswith(prefix):
            rest = p[len(prefix):]
            if up[len(prefix):].startswith("UNC\\"):
                return True
            if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
                return _drive_is_remote(rest[0])
            return True             # a volume GUID, a pipe, a device: not a local file path
    if p.startswith("\\\\") or p.startswith("\\"):
        return p.startswith("\\\\")
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return _drive_is_remote(p[0])
    return False


def _under(target, base):
    return target == base or target.startswith(base.rstrip("\\/") + os.sep)


_INSIDE_CACHE = {}
_INSIDE_TTL = 2.0       # seconds an answer is reused (a save of a 60-database case asks the
                        # same questions hundreds of times in a burst)


def clear_guard_cache():
    """Forget every remembered answer of the write guard (path_inside, _folder_forms): called
    whenever the set of protected folders changes."""
    _INSIDE_CACHE.clear()
    _FOLDER_CACHE.clear()


def path_inside(path, folder):
    """_path_inside, answered again from the last few seconds' answers when the same path and
    folder were just compared (the answer depends on the file system, which a burst of saves
    does not change; after _INSIDE_TTL it is worked out again). A 'not inside' answer for a
    path that does not exist yet is never reused: a folder or link made there in the meantime
    could change it."""
    import time
    key = (path, folder)
    now = time.monotonic()
    hit = _INSIDE_CACHE.get(key)
    if hit is not None and now - hit[1] < _INSIDE_TTL:
        return hit[0]
    result = _path_inside(path, folder)
    try:
        keep = result or os.path.lexists(path)
    except (OSError, ValueError, TypeError):
        keep = False
    if keep:
        if len(_INSIDE_CACHE) > 4096:
            _INSIDE_CACHE.clear()
        _INSIDE_CACHE[key] = (result, now)
    return result


def file_identity(path):
    """(device, inode) of an existing file (following links), or None."""
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return None
    return (st.st_dev, st.st_ino) if any((st.st_dev, st.st_ino)) else None


def linked_file(path, identities=()):
    """True when path is an existing file that writing to would also change a file somewhere
    else: it has several names (hard links, st_nlink > 1: another name may be evidence), or it
    is, under another name, one of `identities` ((device, inode) of the evidence files)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return False
    if not os.path.isfile(path):
        return False
    if st.st_nlink > 1:
        return True
    ident = (st.st_dev, st.st_ino)
    return any(ident) and ident in set(identities)


def inside_any(path, folders):
    """The first of `folders` that `path` is or lies inside (as path_inside decides), or
    None. The path is resolved and its ancestors identified once for all the folders, so a
    case of many evidence folders costs one resolution, not one per folder."""
    folders = [f for f in folders if f]
    if not path or not folders:
        return None
    try:
        forms = [os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.realpath(path))]
    except (OSError, ValueError):
        forms = []
    # the identities (device, inode) of the path's nearest existing ancestor and above
    ids = []
    try:
        cur = os.path.abspath(path)
        while not os.path.exists(cur):
            parent = os.path.dirname(cur)
            if parent == cur:
                cur = None
                break
            cur = parent
        while cur:
            try:
                st = os.stat(cur)
                ids.append((st.st_dev, st.st_ino))
            except OSError:
                pass
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    except (OSError, ValueError):
        pass
    ids = set(ids)
    for folder in folders:
        bases, ident = _folder_forms(folder)
        if any(_under(f, base) for base in bases for f in forms):
            return folder
        if ident is not None and ident in ids and any(ident):
            return folder
    return None


_FOLDER_CACHE = {}


def _folder_forms(folder):
    """(the folder's absolute and resolved forms, its (device, inode) or None), reused for
    _INSIDE_TTL seconds: resolving 60 evidence folders on every save is the slow part."""
    import time
    now = time.monotonic()
    hit = _FOLDER_CACHE.get(folder)
    if hit is not None and now - hit[2] < _INSIDE_TTL:
        return hit[0], hit[1]
    bases = []
    for resolve in (os.path.abspath, os.path.realpath):
        try:
            bases.append(os.path.normcase(resolve(folder)))
        except (OSError, ValueError):
            pass
    try:
        st = os.stat(folder)
        ident = (st.st_dev, st.st_ino)
    except (OSError, ValueError):
        ident = None
    if len(_FOLDER_CACHE) > 4096:
        _FOLDER_CACHE.clear()
    _FOLDER_CACHE[folder] = (bases, ident, now)
    return bases, ident


def _path_inside(path, folder):
    """True when path is folder itself or lies inside it (path need not exist yet).

    Compared as absolute and as resolved paths (links, junctions), then by file identity: the
    nearest existing ancestor of path and each of its own ancestors is compared with folder
    the way os.path.samefile does, which also catches other names for the same folder (a subst
    drive, the \\\\?\\ and \\\\localhost\\c$ forms, 8.3 short names). Never raises: a
    file-system error keeps the answer of the path comparison."""
    if not path or not folder:
        return False
    try:
        for resolve in (os.path.abspath, os.path.realpath):
            if _under(os.path.normcase(resolve(path)), os.path.normcase(resolve(folder))):
                return True
    except (OSError, ValueError):
        pass
    try:
        folder_st = os.stat(folder)
        cur = os.path.abspath(path)
        while not os.path.exists(cur):
            parent = os.path.dirname(cur)
            if parent == cur:
                return False
            cur = parent
        while True:
            try:
                if os.path.samestat(os.stat(cur), folder_st):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(cur)
            if parent == cur:
                return False
            cur = parent
    except (OSError, ValueError):
        return False


def _size_text(n):
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= (1 << shift) and n % (1 << shift) == 0:
            return "%s %s" % (format(n >> shift, ","), unit)
    return "%s bytes" % format(n, ",")


def _file_kind(head):
    if head[:4] in WAL_MAGICS:
        return "WAL copy"
    if head[:8] == JOURNAL_MAGIC:
        return "journal copy"
    if head[:16] == SQLITE_MAGIC:
        return "SQLite database copy"
    return "other"


_HEX = re.compile(r"^[0-9a-fA-F]+$")
_BSD_LINE = re.compile(r"^\s*SHA-?256\s*\((.*)\)\s*=\s*(\S+)\s*$", re.IGNORECASE)
_SUM_LINE = re.compile(r"^\s*\\?(\S+)\s+\*?(.+?)\s*$")
_BARE_LINE = re.compile(r"^\s*(\S+)\s*$")


class HashComparison(list):
    """compare_expected()'s result: one dict per evidence file, plus .unmatched (names listed
    in the pasted text that match no evidence file) and .unparsed ((line, reason) for lines
    that are not a SHA-256 hash)."""

    def __init__(self, items=(), unmatched=(), unparsed=()):
        list.__init__(self, items)
        self.unmatched = list(unmatched)
        self.unparsed = list(unparsed)


def parse_hash_list(text):
    """(named, bare, unparsed) from pasted hashes: named {lower-case base name: hex}, bare [hex]
    (lines holding only a hash), unparsed [(line, reason)]. Accepts sha256sum lines
    ('<hex>  <name>', '<hex> *<name>'), BSD lines ('SHA256 (<name>) = <hex>') and bare hashes,
    in any case. Hashes come back in lower case."""
    named, bare, unparsed = {}, [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = _BSD_LINE.match(line)
        if m:
            name, digest = m.group(1), m.group(2)
        else:
            m = _BARE_LINE.match(line)
            if m:
                digest, name = m.group(1), None
            else:
                m = _SUM_LINE.match(line)
                if not m:
                    unparsed.append((line, "not a hash line"))
                    continue
                digest, name = m.group(1), m.group(2)
        if not _HEX.match(digest):
            unparsed.append((line, "not a hexadecimal hash"))
            continue
        if len(digest) != 64:
            unparsed.append((line, "%d hex digits (a SHA-256 hash has 64)" % len(digest)))
            continue
        digest = digest.lower()
        if name is None:
            bare.append(digest)
            continue
        base = re.split(r"[\\/]", name.strip())[-1].lower()
        if base in named and named[base] != digest:
            unparsed.append((line, "a second, different hash for %s (the first is used)" % base))
            continue
        named[base] = digest
    return named, bare, unparsed


class FileFingerprint(object):
    __slots__ = ("role", "path", "size", "mtime_ns", "sha256", "note")

    def __init__(self, role, path):
        st = os.stat(path)
        self.role, self.path = role, path
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns
        self.sha256 = None
        self.note = ""          # how the hash was made when not plainly (locked bytes)

    def as_dict(self):
        d = {"role": self.role, "path": self.path, "size": self.size,
             "mtime_ns": self.mtime_ns, "sha256": self.sha256}
        if self.note:
            d["sha256_note"] = self.note
        return d


class VerifyReport(object):
    """verify()'s result. checked: what was compared, in words (None: told by rehashed);
    verify_on_close() sets it, and then text() states it even when the evidence changed."""

    def __init__(self, differences, rehashed, checked=None):
        self.differences = differences
        self.rehashed = rehashed
        self.checked = checked

    @property
    def unchanged(self):
        return not self.differences

    def checked_text(self):
        if self.checked:
            return self.checked
        return "size, mtime and SHA-256" if self.rehashed else "size and mtime"

    def text(self):
        if self.unchanged:
            return "Evidence unchanged \u2714 (%s)" % self.checked_text()
        out = "EVIDENCE CHANGED:\n" + "\n".join(self.differences)
        if self.checked:
            out += "\n(checked: %s)" % self.checked
        return out


def _sha256(path, progress=None, cancel=None, notes=None):
    """SHA-256 of the file. The bytes of SQLite's lock-byte range that another program holds
    locked are hashed as zeros (engine.locks), and notes (a list) says so."""
    h = hashlib.sha256()
    pos = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            if cancel is not None and cancel():
                return None
            block = read_range(f, pos, CHUNK, notes)
            if not block:
                break
            h.update(block)
            pos += len(block)
            if progress is not None:
                progress(len(block))
    return h.hexdigest()


class EvidenceSet(object):
    def __init__(self, main_path):
        self.main = os.path.abspath(main_path)
        self.directory = os.path.dirname(self.main)
        self.paths = {"main": self.main}
        for role, suffix in SIDECAR_SUFFIXES:
            p = self.main + suffix
            if os.path.isfile(p):
                self.paths[role] = p
        self.fingerprints = dict((role, FileFingerprint(role, p)) for role, p in self.paths.items())
        self._listing = self._dir_listing()
        self._other, self.other_files_more = self._find_other_files()
        self._thread = None
        self._cancel = False
        self.hashed_bytes = 0
        self.total_bytes = sum(fp.size for fp in self.fingerprints.values())
        self.hash_error = None

    # -- sidecars ------------------------------------------------------
    def path(self, role):
        return self.paths.get(role)

    def journal_is_hot(self):
        """True if a rollback journal with a valid header exists (interrupted transaction)."""
        p = self.paths.get("journal")
        if not p or os.path.getsize(p) < 28:
            return False
        with open(p, "rb") as f:
            head = f.read(8)
        return head == JOURNAL_MAGIC

    # -- other files next to the database -----------------------------------
    def _find_other_files(self):
        if not self._listing:
            return [], 0
        used = set(os.path.normcase(os.path.basename(p)) for p in self.paths.values())
        stem = os.path.normcase(os.path.basename(self.main))
        out, more = [], 0
        for name in self._listing:
            key = os.path.normcase(name)
            if not key.startswith(stem) or key in used:
                continue
            p = os.path.join(self.directory, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            if len(out) >= OTHER_FILES_CAP:
                more += 1
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(16)
            except OSError:
                head = b""
            out.append({"name": name, "path": p, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                        "kind": _file_kind(head)})
        return out, more

    def other_files(self):
        """Files next to the database whose names start with its name but that the tool does
        not read, e.g. 'x.db-wal.bak', 'x.db-wal (1)', 'x.db.bak', 'x.db-journal.old': an
        examiner must know they are there. Each {"name", "path", "size", "mtime_ns", "kind"},
        kind from the first bytes: "WAL copy", "journal copy", "SQLite database copy" or
        "other". Found when the set was made; at most OTHER_FILES_CAP of them
        (other_files_more: how many more there are)."""
        return [dict(d) for d in self._other]

    # -- hashing -------------------------------------------------------
    def start_hashing(self, on_done=None):
        """Hash every evidence file in a background thread (1 MiB chunks, cancellable)."""
        if self._thread is not None:
            return

        def run():
            # a case opens many databases at once: only 'hash_parallel' of them are read for
            # hashing at a time (the others wait their turn, still cancellable)
            slots = _hash_slots()
            while not slots.acquire(timeout=0.2):
                if self._cancel:
                    return
            try:
                for fp in self.fingerprints.values():
                    notes = []
                    try:
                        digest = _sha256(fp.path, self._add_progress, lambda: self._cancel,
                                         notes)
                    except OSError as e:
                        self.hash_error = "%s: %s" % (fp.path, e)
                        return
                    if digest is None:
                        return
                    fp.note = "; ".join(notes)
                    fp.sha256 = digest
            finally:
                slots.release()
            if on_done is not None:
                on_done(self)

        self._thread = threading.Thread(target=run, name="evidence-hash", daemon=True)
        self._thread.start()

    def _add_progress(self, n):
        self.hashed_bytes += n

    @property
    def hashing_done(self):
        return all(fp.sha256 for fp in self.fingerprints.values())

    def wait_hashing(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return self.hashing_done

    def cancel_hashing(self):
        self._cancel = True

    # -- verification --------------------------------------------------
    def _dir_listing(self):
        try:
            return sorted(os.listdir(self.directory))
        except OSError:
            return None

    def verify(self, rehash=False, cancel=None, progress=None):
        """Compare the files with what they were when opened: size and modification time,
        and with rehash the SHA-256 again. cancel() true stops the re-hash (size and time are
        still compared; the report then says the SHA-256 was not re-computed for all files);
        progress(bytes) is called as the files are read."""
        diffs = []
        stopped = False
        for role, fp in self.fingerprints.items():
            try:
                st = os.stat(fp.path)
            except OSError as e:
                diffs.append("%s: cannot stat (%s)" % (fp.path, e))
                continue
            if st.st_size != fp.size:
                diffs.append("%s: size %d -> %d" % (fp.path, fp.size, st.st_size))
            if st.st_mtime_ns != fp.mtime_ns:
                diffs.append("%s: modification time changed" % fp.path)
            if rehash and fp.sha256 and not stopped:
                try:
                    now = _sha256(fp.path, progress, cancel)
                except OSError as e:
                    diffs.append("%s: cannot re-hash (%s)" % (fp.path, e))
                    continue
                if now is None:
                    stopped = True          # the rest: size and time only
                elif now != fp.sha256:
                    diffs.append("%s: SHA-256 changed" % fp.path)
        listing = self._dir_listing()
        if self._listing is not None and listing is not None and listing != self._listing:
            added = sorted(set(listing) - set(self._listing))
            removed = sorted(set(self._listing) - set(listing))
            if added:
                diffs.append("files added in evidence folder: %s" % ", ".join(added))
            if removed:
                diffs.append("files removed from evidence folder: %s" % ", ".join(removed))
        report = VerifyReport(diffs, rehash and not stopped)
        if stopped:
            report.checked = "size and mtime (the SHA-256 re-hash was stopped before the end)"
        return report

    def rehash_on_close(self, rehash_limit_bytes):
        """Whether verify_on_close() re-computes the SHA-256 (hashing finished, files within
        the limit)."""
        return self.hashing_done and self.total_bytes <= rehash_limit_bytes

    def verify_on_close(self, rehash_limit_bytes, cancel=None, progress=None):
        """Verify when closing: SHA-256 is re-computed only when the hashing had finished and
        the files total at most rehash_limit_bytes; otherwise size and mtime only. cancel()
        true skips the rest of the re-hash (size and mtime only; the report says so). The
        report's text() says what was checked."""
        if not self.hashing_done:
            report = self.verify(rehash=False)
            report.checked = "size and mtime (SHA-256 was not computed yet)"
        elif self.total_bytes > rehash_limit_bytes:
            report = self.verify(rehash=False)
            report.checked = ("size and mtime (SHA-256 not re-computed: files larger than %s, "
                              "limit verify_rehash_bytes)" % _size_text(rehash_limit_bytes))
        else:
            report = self.verify(rehash=True, cancel=cancel, progress=progress)
            if report.checked is None:
                report.checked = "size, mtime and SHA-256"
            else:
                report.checked = ("size and mtime (SHA-256 re-hash skipped when closing)")
        return report

    def compare_expected(self, text):
        """Compare pasted hashes with the evidence files' SHA-256. text: sha256sum lines
        ('<hex>  <name>' / '<hex> *<name>'), BSD lines ('SHA256 (<name>) = <hex>') or a bare
        hash, several lines, any case. A listed name is matched to an evidence file by base
        name (case-insensitive); a bare hash is compared with the main file only. Returns a
        HashComparison: per file {"role", "path", "expected", "actual", "result"}, result
        "match", "MISMATCH", "not hashed yet" or "no expected hash given"; .unmatched lists the
        names that match no evidence file, .unparsed the lines that are not SHA-256 hashes."""
        named, bare, unparsed = parse_hash_list(text)
        for extra in bare[1:]:
            unparsed.append((extra, "another bare hash (only one is compared, with the main "
                                    "file: list names to compare the others)"))
        items, used = [], set()
        for role, fp in self.fingerprints.items():
            base = os.path.basename(fp.path).lower()
            expected = named.get(base)
            if expected is not None:
                used.add(base)
                if role == "main" and bare:
                    unparsed.append((bare[0], "bare hash not compared: the main file is "
                                              "listed by name"))
            elif role == "main" and bare:
                expected = bare[0]
            actual = fp.sha256
            if expected is None:
                result = "no expected hash given"
            elif not actual:
                result = "not hashed yet"
            else:
                result = "match" if actual.lower() == expected else "MISMATCH"
            items.append({"role": role, "path": fp.path, "expected": expected,
                          "actual": actual, "result": result})
        unmatched = sorted(n for n in named if n not in used)
        return HashComparison(items, unmatched, unparsed)

    # -- write guard ---------------------------------------------------
    def identities(self):
        """(device, inode) of each evidence file (the database and its sidecars)."""
        ids = getattr(self, "_identities", None)
        if ids is None:
            ids = self._identities = [i for i in (file_identity(p) for p in self.paths.values())
                                      if i is not None]
        return ids

    def is_protected(self, path):
        """True if writing to `path` would put a file inside the evidence directory (under
        any of its names: see path_inside), or would change an evidence file or any file
        with several names (a hard link: see linked_file)."""
        return path_inside(path, self.directory) or linked_file(path, self.identities())

    def summary(self):
        return [fp.as_dict() for fp in self.fingerprints.values()]
