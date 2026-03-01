#!/usr/bin/env python3
"""SQLite Forensic Analyzer — entry point.

Works both as a normal Python script and as a PyInstaller-frozen executable.
"""

import sys
import os

# Determine base directory (handles PyInstaller --onefile temp extraction)
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
else:
    _base = os.path.dirname(os.path.abspath(__file__))

# Add src/ to path so modules can import each other
sys.path.insert(0, os.path.join(_base, "src"))

from app import App

if __name__ == "__main__":
    app = App(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()
