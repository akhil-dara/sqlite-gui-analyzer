# Third-party notices

SQLite GUI Analyzer is written against the Python standard library alone: Tkinter for the
window, `sqlite3` for SQL on the databases it opens, and the rest of the library for files,
hashing, threads, JSON and plists. Running from source needs nothing else.

The Windows builds bundle, and the optional image previews use, the following software.
Each is the work of its own authors and carries its own license, reproduced in the files
named below inside the installed program folder (`_internal/`) and in the portable zip.

| Component | Used for | License |
|---|---|---|
| [Python](https://www.python.org/) and its standard library | the interpreter the builds ship with | Python Software Foundation License |
| [Tcl/Tk](https://www.tcl.tk/) | the toolkit behind Tkinter | BSD-style (Tcl/Tk license) |
| [SQLite](https://sqlite.org/) | the `sqlite3` module's library | Public domain |
| [Pillow](https://python-pillow.org/) (optional) | JPEG, WEBP and other image previews in the BLOB Inspector and the picture cells | MIT-CMU (HPND) |
| [PyInstaller](https://pyinstaller.org/) | packaging the Windows builds; its bootloader is compiled from source at build time | GPL 2.0 with the bootloader exception (the packaged program is not affected) |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | the Windows installer | Inno Setup License |

The program never sends data anywhere: it does not connect to the network, and nothing
it does leaves the computer it runs on.

The versions bundled by a given release are pinned, with the SHA-256 of each file, in
`tools/build-requirements.txt`.
