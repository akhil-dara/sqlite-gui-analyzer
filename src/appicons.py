"""The app's logo images (src/assets/logo_<size>.png, PNG read by Tk 8.6 itself: no imaging
library needed). In the frozen app the folder is bundled as 'assets' next to the code."""

import os
import sys
import tkinter as tk

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def assets_dir():
    """The folder holding the logo files (source tree or the frozen bundle)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def logo_path(size):
    """The logo file closest to `size` pixels (the next larger one when between sizes)."""
    best = next((s for s in SIZES if s >= size), SIZES[-1])
    return os.path.join(assets_dir(), "logo_%d.png" % best)


def logo_image(master, size):
    """A PhotoImage of the logo about `size` pixels high, or None when it cannot be read.
    Keep a reference to it: Tk drops an image Python no longer holds."""
    path = logo_path(size)
    try:
        return tk.PhotoImage(master=master, file=path)
    except (tk.TclError, OSError):
        return None


def illustration(master, name, width=None):
    """A PhotoImage of an empty-state illustration (src/assets/<name>.png, transparent),
    shrunk by a whole factor to about `width` pixels; None when it cannot be read."""
    path = os.path.join(assets_dir(), name + ".png")
    try:
        img = tk.PhotoImage(master=master, file=path)
    except (tk.TclError, OSError):
        return None
    if width and img.width() > width:
        factor = -(-img.width() // width)          # subsample shrinks by whole factors
        img = img.subsample(factor, factor)
    return img


def window_icons(master):
    """PhotoImages for the window and taskbar icon, largest first (wm iconphoto picks)."""
    out = []
    for s in (256, 64, 48, 32, 16):
        img = logo_image(master, s)
        if img is not None:
            out.append(img)
    return out
