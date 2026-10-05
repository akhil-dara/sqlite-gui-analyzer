"""One sanitizer for every file name the app derives from data (table, tag, database names).

The result is a single path component that is safe on Windows and elsewhere:
- no path separator, ':' or other character Windows refuses, no control character;
- no bidirectional or other format character (Unicode category Cf, e.g. U+202E RIGHT-TO-LEFT
  OVERRIDE, which can make "evil‮gpj.exe" look like "evilexe.jpg");
- never a reserved Windows device name (CON, PRN, AUX, NUL, COM0-9, LPT0-9, also with the
  superscript digits ¹ ² ³, CONIN$ / CONOUT$), whatever the extension: "NUL.csv" opens the
  NUL device on Windows 10 and older, so the data would be lost. Such a name gets a '_' prefix;
- no trailing dots or spaces (Windows drops them), never '', '.' or '..'.
"""

import re
import unicodedata

_STRICT = re.compile(r"[^\w.\-]+")                      # letters, digits, '_', '.', '-'
_WINDOWS_REFUSED = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["%s%s" % (p, d) for p in ("COM", "LPT") for d in "0123456789¹²³"])


def _drop_format_chars(text):
    """Remove format (Cf: bidi overrides, zero-width marks), control and surrogate characters."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc", "Cs"))


def is_reserved_name(name):
    """True when Windows maps name to a device: its part before the first '.', without trailing
    spaces, is CON, PRN, AUX, NUL, COM0-9, LPT0-9 (also COM¹ etc.), CONIN$ or CONOUT$, in any case."""
    stem = str(name).split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED


def safe_file_name(text, limit=80, default="x", strip="_", strict=True):
    """text as one safe file-name component (see the module docstring), at most limit characters.

    strict=True keeps only letters, digits, '_', '.' and '-' (anything else becomes '_');
    strict=False replaces only the characters Windows refuses, so spaces and punctuation stay.
    strip: characters removed from both ends (trailing dots and spaces always go).
    default: the name used when nothing usable is left."""
    limit = max(int(limit), 2)
    name = _drop_format_chars(str(text))
    name = (_STRICT if strict else _WINDOWS_REFUSED).sub("_", name)

    def tidy(s):
        while True:
            t = (s.strip(strip) if strip else s).rstrip(". ")
            if t == s:
                return t
            s = t

    name = tidy(name)
    name = tidy(name[:limit])
    if name in ("", ".", ".."):
        name = default
    if is_reserved_name(name):
        name = "_" + name[:limit - 1]
        name = name.rstrip(". ")
    return name
