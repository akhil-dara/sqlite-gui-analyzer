# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of SQLite GUI Analyzer (Windows, windowed).

    pip install -r tools/build-requirements.txt
    python -m PyInstaller --noconfirm --clean --distpath DIST --workpath WORK sqlite-gui-analyzer.spec

Keep DIST and WORK outside the source tree. The build produces, in DIST:
    SQLiteGUIAnalyzer/SQLiteGUIAnalyzer.exe    onedir build: starts fast; the installer and the
                                               portable zip are made from this folder
    SQLiteGUIAnalyzer-<version>-portable.exe   onefile build: one self-extracting exe
The version is VERSION in src/constants.py; the Windows version resource is generated from it.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(SPECPATH, "tools"))
_write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no __pycache__ in tools/
try:
    import release  # noqa: E402
finally:
    sys.dont_write_bytecode = _write_bytecode

VERSION = release.read_version(SPECPATH)
APP_ID = release.APP_ID
VERSION_FILE = release.write_version_resource(os.path.join(workpath, "version_info.txt"), VERSION)
ICON = os.path.join(SPECPATH, "icon.ico")


def available(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        return False


# Running from source Pillow is optional; the built programs always bundle it (JPEG/WEBP previews).
if not available("PIL"):
    raise SystemExit("Pillow is required for the build: pip install -r tools/build-requirements.txt")

# Pillow's Tk bridge is imported lazily (ImageTk -> _imagingtk): name it so it is always bundled.
HIDDEN_IMPORTS = [m for m in ("PIL.ImageTk", "PIL._imagingtk", "PIL._tkinter_finder") if available(m)]

# Never used by the app; excluded so a build environment with more installed stays small.
# PIL._avif is Pillow's AVIF codec (7.5 MB): previews cover JPEG, PNG, GIF, BMP and WEBP.
EXCLUDES = [
    "unittest", "doctest", "pdb", "pydoc", "pydoc_data", "lib2to3", "distutils", "setuptools",
    "pkg_resources", "ensurepip", "venv", "idlelib", "turtle", "turtledemo", "tkinter.test",
    "test", "xmlrpc", "numpy", "IPython", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "PIL._avif",
]

a = Analysis(
    [os.path.join(SPECPATH, "sqlite_gui_analyzer.py")],
    pathex=[os.path.join(SPECPATH, "src")],
    binaries=[],
    # the logo images (window icon, header): appicons.assets_dir() reads them from 'assets'
    datas=[(os.path.join(SPECPATH, "src", "assets"), "assets")],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)


def unused_tcl_data(dest):
    """Tcl's time-zone database and clock translations serve only Tcl's `clock` command, which
    the app never uses: about 740 of 960 files, and the onefile exe unpacks every file on start."""
    dest = dest.replace("\\", "/")
    return dest.startswith(("_tcl_data/tzdata/", "_tcl_data/msgs/"))


a.datas = [entry for entry in a.datas if not unused_tcl_data(entry[0])]
pyz = PYZ(a.pure)

EXE_OPTIONS = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX-packed DLLs break more often and trip virus scanners
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_FILE,
    icon=[ICON],
)

# onefile: SQLiteGUIAnalyzer-<version>-portable.exe
portable_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="%s-%s-portable" % (APP_ID, VERSION),
    runtime_tmpdir=None,
    **EXE_OPTIONS
)

# onedir: SQLiteGUIAnalyzer/SQLiteGUIAnalyzer.exe (+ _internal/)
onedir_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_ID,
    **EXE_OPTIONS
)
onedir = COLLECT(
    onedir_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_ID,
)
