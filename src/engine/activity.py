"""The examiner's activity log: what was done during an examination, in order, with the time.

One append-only JSON-lines file per case (the same databases always give the same file) in the
app-data folder: activity/activity-<n>db-<12 hex of sha1(the sorted normalised paths)>.jsonl.
Each line is {"utc": "YYYY-MM-DD HH:MM:SS.ffffff UTC", "kind": ..., <details>}. Kinds the UI
uses: open, hash, verify, close, search, export, tag, note (any kind is accepted).

Lines are only ever added, one write per line, under a lock (several threads may log at once).
Logging never raises: a failure is kept in .error and log() returns False. Nothing is written
inside an evidence folder (refuse_in), however the app-data folder is configured.

No Tk here.
"""

import datetime
import hashlib
import json
import math
import os
import threading
from collections import OrderedDict, deque

from .csvcells import csv_writer
from .evidence import inside_any
from .tags import data_dir

ACTIVITY_FORMAT = "sqlite-gui-analyzer-activity"
ACTIVITY_VERSION = 1
EXPORT_FORMATS = ("csv", "json", "txt")
KINDS = ("open", "hash", "verify", "close", "search", "export", "tag", "note")
_RESERVED = ("utc", "kind")


_locks = {}
_locks_guard = threading.Lock()


def _lock_for(path):
    """One lock per log file, shared by every ActivityLog of this process writing it."""
    key = os.path.normcase(os.path.abspath(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


class ActivityError(Exception):
    """The activity log cannot be exported where asked (e.g. inside an evidence folder)."""


def log_path(db_paths, directory=None):
    """activity/activity-<n>db-<12 hex of sha1(the sorted normalised paths)>.jsonl in the
    app-data folder (or directory): the same databases always give the same file."""
    norm = sorted(os.path.normcase(os.path.abspath(p)) for p in db_paths)
    digest = hashlib.sha1("\n".join(norm).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return os.path.join(directory or data_dir(), "activity",
                        "activity-%ddb-%s.jsonl" % (len(norm), digest))


def utc_stamp():
    """Now as 'YYYY-MM-DD HH:MM:SS.ffffff UTC'."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def json_safe(v):
    """v with everything JSON cannot carry made plain: bytes as hex, non-finite floats as
    their repr, dict keys as text, tuples and sets as lists, other objects as str()."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, dict):
        return OrderedDict((str(k), json_safe(x)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((json_safe(x) for x in v), key=str)
    return str(v)


def entry_details(entry):
    """An entry's fields other than utc and kind."""
    return OrderedDict((k, v) for k, v in entry.items() if k not in _RESERVED)


def entry_text(entry):
    """One readable line for an entry: '<utc>  <kind>  key=value, ...'."""
    parts = []
    for k, v in entry_details(entry).items():
        parts.append("%s=%s" % (k, v if isinstance(v, str)
                                else json.dumps(v, ensure_ascii=False)))
    return "%s  %s  %s" % (entry.get("utc", ""), entry.get("kind", ""), ", ".join(parts))


class ActivityLog(object):
    """The activity log of the case made of db_paths. refuse_in: evidence folders nothing may
    be written into (the log file, or an export of it)."""

    def __init__(self, db_paths, directory=None, refuse_in=()):
        self.db_paths = [os.path.abspath(p) for p in db_paths]
        self.path = log_path(db_paths, directory)
        self.refuse_in = [f for f in (refuse_in or ()) if f]
        self.error = None
        self._lock = _lock_for(self.path)

    def _refused(self, path):
        """The evidence folder path lies in (None: none). The answer for the log's own file is
        worked out once: a case of 60 databases would otherwise resolve 60 folders for every
        entry."""
        cache = self.__dict__.setdefault("_refused_cache", {})
        if path in cache:
            return cache[path]
        found = inside_any(path, self.refuse_in)
        cache[path] = found
        return found

    def log(self, kind, **fields):
        """Append one entry. Returns True when written; else False with the reason in
        .error. Never raises."""
        return self.log_many([(kind, fields)])

    def log_many(self, items):
        """Append several entries ((kind, fields) pairs) in one write: a case of 60 databases
        notes 60 openings at once, and one file open instead of 60 keeps the window
        responsive. Returns True when written; else False with the reason in .error. Never
        raises."""
        try:
            if not items:
                return True
            line = b""
            for kind, fields in items:
                entry = OrderedDict([("utc", utc_stamp()), ("kind", str(kind))])
                for k, v in fields.items():
                    entry[("field_" + k) if k in _RESERVED else k] = json_safe(v)
                line += (json.dumps(entry, ensure_ascii=False) + "\n").encode(
                    "utf-8", "backslashreplace")
            folder = self._refused(self.path)
            if folder is not None:
                self.error = ("not logged: the activity log %s would be inside the evidence "
                              "folder %s" % (self.path, folder))
                return False
            with self._lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a+b") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell() > 0:        # a torn last line (a crash mid-write) is closed off
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            line = b"\n" + line
                        f.seek(0, os.SEEK_END)
                    f.write(line)
                    f.flush()
            self.error = None
            return True
        except Exception as e:  # noqa: BLE001 - logging must never break the caller
            self.error = "not logged: %s" % e
            return False

    @staticmethod
    def _tail(f, limit):
        """The last `limit` complete lines of the open binary file f (it stays readable
        from the start; a torn first line is dropped by the caller)."""
        f.seek(0, 2)
        size = f.tell()
        # average entry line with headroom; grow until enough newlines are seen
        chunk = max(65536, limit * 512)
        pos = size
        buf = b""
        while pos > 0 and buf.count(b"\n") <= limit:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            chunk *= 2
        lines = buf.split(b"\n")
        if lines and not lines[-1].strip():
            lines.pop()  # the file ends with a newline: no empty last entry
        # drop a possibly torn first line (unless the whole file was read)
        if pos > 0 and lines:
            lines = lines[1:]
        return b"\n".join(lines[-limit:])

    def entries(self, limit=None):
        """The entries in the file, oldest first (with limit: the last `limit` of them). A line
        that is not valid JSON (e.g. torn by a crash) is skipped. With a limit the file is
        read from the end, so a log grown over the tool's lifetime never loads wholly."""
        out = deque(maxlen=limit) if limit else []
        try:
            with self._lock:
                with open(self.path, "rb") as f:
                    data = self._tail(f, limit) if limit else f.read()
        except OSError:
            return []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                e = json.loads(raw.decode("utf-8", "replace"), object_pairs_hook=OrderedDict)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return list(out)

    def export(self, path, fmt):
        """Write the entries to path as "csv" (utc, kind, details as JSON), "json" or "txt"
        (one readable line each). Refused (ActivityError) inside an evidence folder. Returns
        the number of entries written."""
        if fmt not in EXPORT_FORMATS:
            raise ActivityError("unknown format %r (csv, json or txt)" % (fmt,))
        if not path:
            raise ActivityError("no export location given")
        path = os.path.abspath(path)
        folder = self._refused(path)
        if folder is not None:
            raise ActivityError("refusing to write %s: it is inside the evidence folder %s"
                                % (path, folder))
        entries = self.entries()
        try:
            f = open(path, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8",
                     newline="" if fmt == "csv" else "\n")
        except OSError as e:
            raise ActivityError("cannot write %s: %s" % (path, e))
        try:
            with f:
                if fmt == "csv":
                    w = csv_writer(f)    # spreadsheet-safe text (engine.csvcells)
                    w.writerow(["utc", "kind", "details"])
                    for e in entries:
                        w.writerow([e.get("utc", ""), e.get("kind", ""),
                                    json.dumps(entry_details(e), ensure_ascii=False)])
                elif fmt == "json":
                    data = OrderedDict([("format", ACTIVITY_FORMAT),
                                        ("version", ACTIVITY_VERSION), ("log", self.path),
                                        ("databases", self.db_paths),
                                        ("exported_utc", utc_stamp()), ("entries", entries)])
                    json.dump(data, f, ensure_ascii=False, indent=1)
                    f.write("\n")
                else:
                    for e in entries:
                        f.write(entry_text(e) + "\n")
        except Exception as e:  # noqa: BLE001 - any failure removes the partial file
            try:
                os.remove(path)
                what = "the partly written file was removed"
            except OSError:
                what = "the partly written file is INCOMPLETE"
            raise ActivityError("cannot write %s: %s (%s)" % (path, e, what))
        return len(entries)
