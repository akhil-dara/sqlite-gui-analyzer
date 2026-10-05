"""Shared constants for SQLite GUI Analyzer."""

from collections import OrderedDict

# The one version source: the window title, About/Help, the schema report, the PyInstaller
# builds, the installer and pyproject.toml all read it (see tools/release.py).
VERSION = "2.1.0"

# Windows taskbar icon fix — show app icon instead of Python icon
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('sqlite.gui.analyzer.v1')
except Exception:
    pass

try:
    from PIL import Image as PILImage, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    PILImage = None
    ImageTk = None

# ── colours: the older names, as design tokens (tokens.py) ──────────────────
from tokens import COLOR as _K   # noqa: E402

C = dict(
    bg=_K["card"], bg2=_K["background"], bg3=_K["muted"], bg4=_K["hover"],
    border=_K["border"], text=_K["text"], text2=_K["muted_text"],
    accent=_K["primary"], acl=_K["primary_soft"], green=_K["success_text"],
    gl=_K["success_soft"], red=_K["danger"], rl=_K["danger_soft"], yellow=_K["accent"],
    orange=_K["warning"], purple=_K["purple"], tsel=_K["selection"], alt=_K["row_alt"],
    hl=_K["highlight"], hbg=_K["card"], hfg=_K["heading"], sbg=_K["background"],
)

# ── search modes ─────────────────────────────────────────────────────────
# the nine modes, grouped: text (6), binary (2), schema (1). Earlier names still work.
SEARCH_MODES = OrderedDict([
    ("Case-Insensitive", "ci"),
    ("Case-Sensitive", "cs"),
    ("Exact Match", "ex"),
    ("Starts With", "sw"),
    ("Ends With", "ew"),
    ("Regex", "rx"),
    ("Text in BLOBs", "blob"),
    ("Byte pattern (hex)", "hex"),
    ("Column Name", "col"),
])
SEARCH_MODE_GROUPS = (("Text", ("ci", "cs", "ex", "sw", "ew", "rx")),
                      ("Binary", ("blob", "hex")), ("Schema", ("col",)))
_MODE_ALIASES = {"BLOB/Hex": "blob", "Hex Bytes": "hex"}


def search_mode_key(label, default="ci"):
    """Engine mode key of a mode label (current or earlier name) or of a key itself."""
    if label in SEARCH_MODES:
        return SEARCH_MODES[label]
    if label in _MODE_ALIASES:
        return _MODE_ALIASES[label]
    return label if label in SEARCH_MODES.values() else default


# a search hit's internal source key -> the name the UI shows (tag files keep the key)
SOURCE_NAMES = {"Freelist": "Freed pages"}


def source_name(source):
    """How a source is shown: 'Freelist' (the internal key) reads 'Freed pages'."""
    s = str(source or "")
    for key, name in SOURCE_NAMES.items():
        if s.startswith(key):
            return name + s[len(key):]
    return s


# how a database was opened (engine.session modes): one name everywhere (the case chips,
# Info, Evidence, row details); short for the chips, long where there is room
MODE_LABELS = {
    "immutable": ("immutable", "immutable (no WAL to merge)"),
    "ram-overlay": ("WAL merged", "WAL merged in RAM (SQL sees the current state)"),
    "main-only": ("WAL not in SQL", "WAL not in SQL (SQL sees the main file only)"),
    "native": ("read natively", "read natively (SQLite could not open it)"),
    "safe-parse": ("Safe parse", "Safe parse (only the built-in parser reads it; SQLite is "
                                   "not used)"),
}


def mode_label(mode, long=False):
    """The name of an open mode ('ram-overlay' reads 'WAL merged'); unknown ones as they
    are."""
    names = MODE_LABELS.get(str(mode or ""))
    return names[1 if long else 0] if names else str(mode or "")


def source_key(label):
    """The internal source key of a label source_name() shows ('Freed pages' -> 'Freelist');
    a key or any other text comes back unchanged."""
    s = str(label or "")
    for key, name in SOURCE_NAMES.items():
        if s.startswith(name):
            return key + s[len(name):]
    return s

# ── blob signatures ──────────────────────────────────────────────────────
_SIGS = [
    (b'\xff\xd8\xff',         "JPEG"),
    (b'\x89PNG\r\n\x1a\n',   "PNG"),
    (b'GIF87a',               "GIF"),
    (b'GIF89a',               "GIF"),
    (b'RIFF',                 "RIFF"),
    (b'bplist',               "bplist"),
    (b'<?xml',                "XML/Plist"),
    (b'SQLite format 3',      "SQLite"),
    (b'%PDF',                 "PDF"),
    (b'PK\x03\x04',          "ZIP"),
    (b'\x1f\x8b',            "GZIP"),
    (b'II\x2a\x00',          "TIFF"),
    (b'MM\x00\x2a',          "TIFF"),
    (b'OggS',                 "OGG"),
    (b'\xff\xfb',            "MP3"),
    (b'\xff\xf3',            "MP3"),
    (b'\xff\xf2',            "MP3"),
    (b'ID3',                  "MP3"),
    (b'\x1a\x45\xdf\xa3',   "MKV/WEBM"),
    (b'\x00\x00\x01\x00',   "ICO"),
    (b'BM',                   "BMP"),
    (b'\x00asm',             "WASM"),
    (b'\x7fELF',             "ELF"),
    (b'MZ',                   "PE/EXE"),
    (b'\xfe\xed\xfa\xce',   "Mach-O"),
    (b'\xfe\xed\xfa\xcf',   "Mach-O"),
    (b'\xce\xfa\xed\xfe',   "Mach-O"),
    (b'\xcf\xfa\xed\xfe',   "Mach-O"),
    (b'dex\n',               "DEX"),
]

_EXT_MAP = {
    "JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp",
    "bplist": ".plist", "XML/Plist": ".plist", "SQLite": ".sqlite",
    "PDF": ".pdf", "ZIP": ".zip", "GZIP": ".gz", "TIFF": ".tif",
    "OGG": ".ogg", "MP3": ".mp3", "MKV/WEBM": ".mkv", "ICO": ".ico",
    "MP4": ".mp4", "HEIF": ".heif", "BMP": ".bmp", "WASM": ".wasm",
    "ELF": "", "PE/EXE": ".exe", "Mach-O": "", "DEX": ".dex",
    "RIFF": ".riff",
}

# ── WAL constants ────────────────────────────────────────────────────────
WAL_MAGIC_BE = 0x377f0682
WAL_MAGIC_LE = 0x377f0683
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
PAGE_TYPES = {
    0x02: "Index Interior",
    0x05: "Table Interior",
    0x0A: "Index Leaf",
    0x0D: "Table Leaf",
    0x00: "Overflow / Free",
}

# WAL frame states (engine.fileformat.wal): key -> (label, colour, row background, meaning)
WAL_STATES = OrderedDict([
    ("current", ("Current", _K["success_text"], _K["success_soft"],
                 "Committed; latest version of this page (what SQLite shows)")),
    ("superseded", ("Superseded", _K["purple"], _K["purple_soft"],
                    "Committed, then replaced by a later commit (older version)")),
    ("uncommitted", ("Uncommitted", _K["warning"], _K["warning_soft"],
                     "Written after the last commit (in progress or rolled back); invisible to SQLite")),
    ("stale", ("Stale", _K["danger_text"], _K["danger_soft"],
               "Left from an earlier WAL generation (salt mismatch); older data")),
])


def wal_state_label(state):
    return WAL_STATES.get(state, (state,))[0]


def refresh_colors():
    """Rebuild C and ROW_FLAG_BG from the current tokens, in place, so every module that
    did `from constants import C` follows a tokens.set_theme() switch."""
    C.clear()
    C.update(dict(
        bg=_K["card"], bg2=_K["background"], bg3=_K["muted"], bg4=_K["hover"],
        border=_K["border"], text=_K["text"], text2=_K["muted_text"],
        accent=_K["primary"], acl=_K["primary_soft"], green=_K["success_text"],
        gl=_K["success_soft"], red=_K["danger"], rl=_K["danger_soft"], yellow=_K["accent"],
        orange=_K["warning"], purple=_K["purple"], tsel=_K["selection"], alt=_K["row_alt"],
        hl=_K["highlight"], hbg=_K["card"], hfg=_K["heading"], sbg=_K["background"],
        wal_bg=_K["purple_soft"],
    ))
    ROW_FLAG_BG.clear()
    ROW_FLAG_BG.update({"flag_damaged": _K["danger_soft"],
                        "flag_prealter": _K["warning_soft"]})


# Row backgrounds for engine row flags (utils.row_flag_tag -> colour): refreshed in place
# by refresh_colors(), so a theme switch repaints them too.
ROW_FLAG_BG = {}
refresh_colors()

# Row backgrounds for engine row flags (utils.row_flag_tag -> colour): rebuilt by
# refresh_colors() above, in place, on every theme switch.
