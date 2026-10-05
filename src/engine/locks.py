"""Evidence held by another program (Windows): byte-range locks and sharing violations.

- A live SQLite user holds byte-range locks on the database's lock-byte page (the page that
  starts at 1 GiB; SQLite never stores data there). Reading those bytes with ReadFile then
  fails. read_range() reads a file range and gives the locked lock-byte bytes as zeros, saying
  so in a note, instead of failing.
- A program that opened the file without read sharing makes every open fail with a sharing
  violation. users_of() asks the Restart Manager (no administrator rights needed) which
  programs have the file open, so the error can say 'in use by <program> (PID n)'.

Nothing here writes, and nothing is created: the Restart Manager session only lists.
"""

import errno
import os
import sys

LOCK_BYTE = 0x40000000          # SQLite's PENDING_BYTE: the lock-byte page starts here
LOCK_SPAN = 512                 # pending, reserved and shared lock bytes (SQLite on Windows)
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33


def _overlaps_lock(pos, n):
    return pos < LOCK_BYTE + LOCK_SPAN and pos + n > LOCK_BYTE


def _is_lock_error(e):
    return (getattr(e, "winerror", None) == ERROR_LOCK_VIOLATION
            or (os.name == "nt" and e.errno == errno.EACCES))


def _read_exact(f, pos, n):
    f.seek(pos)
    parts, left = [], n
    while left > 0:
        b = f.read(left)
        if not b:
            break
        parts.append(b)
        left -= len(b)
    return b"".join(parts)


def read_range(f, pos, n, notes=None):
    """n bytes of the unbuffered binary file f from pos (fewer at its end). Bytes of the
    lock-byte range another program holds locked come back as zeros, and notes (a list)
    gets one line saying so."""
    try:
        return _read_exact(f, pos, n)
    except OSError as e:
        if not (_is_lock_error(e) and _overlaps_lock(pos, n)):
            raise
    out = []
    if pos < LOCK_BYTE:
        out.append(_read_exact(f, pos, LOCK_BYTE - pos))
    lo, hi = max(pos, LOCK_BYTE), min(pos + n, LOCK_BYTE + LOCK_SPAN)
    size = os.fstat(f.fileno()).st_size
    zeros = max(0, min(hi, size) - lo)
    out.append(b"\x00" * zeros)
    if notes is not None and zeros:
        notes.append("bytes %s-%s (SQLite's lock-byte page, which never holds data) are locked "
                     "by another program and were read as zeros"
                     % (format(lo, ","), format(lo + zeros - 1, ",")))
    if pos + n > hi and hi < size:
        out.append(_read_exact(f, hi, pos + n - hi))
    return b"".join(out)


def is_sharing_violation(e, path=None):
    """True for an open refused because another program holds the file. Python's open()
    reports a sharing violation as plain EACCES on Windows, so with a path the Restart
    Manager is asked whether any program has it open."""
    if getattr(e, "winerror", None) == ERROR_SHARING_VIOLATION:
        return True
    return (sys.platform == "win32" and path is not None
            and getattr(e, "errno", None) == errno.EACCES and bool(users_of(path)))


def users_of(path):
    """[(program name, PID)] that have `path` open, from the Windows Restart Manager; [] when
    it cannot tell (another OS, an error)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes as wt
    except ImportError:
        return []

    class _UniqueProcess(ctypes.Structure):
        _fields_ = [("dwProcessId", wt.DWORD), ("ProcessStartTime", wt.FILETIME)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("Process", _UniqueProcess), ("strAppName", ctypes.c_wchar * 256),
                    ("strServiceShortName", ctypes.c_wchar * 64), ("ApplicationType", ctypes.c_int),
                    ("AppStatus", wt.ULONG), ("TSSessionId", wt.DWORD),
                    ("bRestartable", wt.BOOL)]

    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return []
    rm.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.c_wchar_p]
    rm.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT, ctypes.POINTER(ctypes.c_wchar_p),
                                       wt.UINT, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
    rm.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT),
                             ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    rm.RmEndSession.argtypes = [wt.DWORD]
    handle = wt.DWORD()
    key = ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(handle), 0, key) != 0:
        return []
    try:
        files = (ctypes.c_wchar_p * 1)(os.path.abspath(path))
        if rm.RmRegisterResources(handle, 1, files, 0, None, 0, None) != 0:
            return []
        out = []
        count = 16
        for _attempt in range(4):
            needed, got = wt.UINT(0), wt.UINT(count)
            reasons = wt.DWORD()
            infos = (_ProcessInfo * count)()
            r = rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(got), infos,
                             ctypes.byref(reasons))
            if r == 234:                    # ERROR_MORE_DATA: ask again with more room
                count = min(4096, needed.value + 4)
                continue
            if r != 0:
                return []
            for i in range(got.value):
                out.append((infos[i].strAppName or "a program", int(infos[i].Process.dwProcessId)))
            break
        return out
    except Exception:       # noqa: BLE001 - only an explanation is lost
        return []
    finally:
        rm.RmEndSession(handle)


def in_use_text(path):
    """'in use by Program (PID n), ...' for a file another program holds open, or ''."""
    users = users_of(path)
    if not users:
        return ""
    return "in use by %s" % ", ".join("%s (PID %d)" % u for u in users[:5])


def sharing_advice(path):
    """The full explanation of a sharing violation on `path`."""
    who = in_use_text(path) or "in use by another program"
    return ("%s is %s, which opened it without letting others read it. Close that program, "
            "or read a copy made with a forensic acquisition tool or from a Volume Shadow "
            "Copy." % (os.path.basename(path), who))
