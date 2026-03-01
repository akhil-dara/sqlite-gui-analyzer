"""Shared constants for SQLite GUI Analyzer."""

from collections import OrderedDict

VERSION = "1.0"

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

# ── colours ──────────────────────────────────────────────────────────────
C = dict(
    bg="#ffffff", bg2="#f7f8fa", bg3="#eef0f4", bg4="#dfe2e8",
    border="#c8cdd5", text="#1a1a2e", text2="#5e6c84",
    accent="#0052cc", acl="#deebff", green="#00875a", gl="#e3fcef",
    red="#de350b", rl="#ffebe6", yellow="#ff991f", orange="#c25100",
    purple="#6554c0", tsel="#cce0ff", alt="#f8f9fb", hl="#fff0b3",
    hbg="#0747a6", hfg="#ffffff", sbg="#f4f5f7",
)

# ── search modes ─────────────────────────────────────────────────────────
SEARCH_MODES = OrderedDict([
    ("Case-Insensitive", "ci"),
    ("Case-Sensitive", "cs"),
    ("Exact Match", "ex"),
    ("Starts With", "sw"),
    ("Ends With", "ew"),
    ("Regex", "rx"),
    ("BLOB/Hex", "blob"),
    ("Column Name", "col"),
])

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

# WAL frame category colours
C.update({
    "wal_committed": "#00875a",    # green — safe, in DB
    "wal_uncommitted": "#ff991f",  # yellow/orange — pending
    "wal_old": "#de350b",          # red — pre-checkpoint, potentially lost
    "wal_bg": "#f5f0ff",           # light purple background for WAL results
})
