#!/usr/bin/env python3
"""SQLite Forensic Analyzer — entry point.

Works both as a normal Python script and as a PyInstaller-frozen executable.
"""

import sys
import os

def main():
    if getattr(sys, "frozen", False):
        _base = sys._MEIPASS
    else:
        _base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(_base, "src"))
    from app import App
    app = App(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()


if __name__ == "__main__":
    main()
