"""Reusable UI widgets for SQLite GUI Analyzer."""

import sys
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

from constants import C


# ── tooltip helper ────────────────────────────────────────────────────────
class ToolTip:
    """Lightweight tooltip for any widget."""
    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._hide()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=self.text, bg="#333333", fg="#ffffff",
                       font=("Segoe UI", 8), padx=6, pady=3, relief="solid", bd=1,
                       wraplength=300, justify="left")
        lbl.pack()

    def _hide(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ── treeview cell tooltip ────────────────────────────────────────────────
class TreeviewTooltip:
    """Hover tooltip for Treeview cells — shows full cell text when truncated."""
    def __init__(self, tree, delay=400):
        self.tree = tree
        self.delay = delay
        self._tip = None
        self._after_id = None
        self._last_cell = (None, None)
        self._font = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<ButtonPress>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _on_motion(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        cell = (row, col)
        if cell == self._last_cell:
            return
        self._last_cell = cell
        self._hide()
        if row and col:
            self._after_id = self.tree.after(self.delay, lambda: self._show(row, col, event))

    def _show(self, row, col, event):
        if not self.tree.winfo_exists():
            return
        try:
            col_idx = int(col.replace("#", "")) - 1
            values = self.tree.item(row, "values")
            if col_idx < 0 or col_idx >= len(values):
                return
            text = str(values[col_idx])
            if not text or len(text) < 10:
                return
            # Measure actual text width using font
            if not self._font:
                self._font = tkfont.nametofont("TkDefaultFont")
            col_id = self.tree.cget("columns")[col_idx] if self.tree.cget("columns") else col
            col_width = self.tree.column(col_id, "width")
            text_px = self._font.measure(text)
            if text_px <= col_width - 20:
                return  # fits — no tooltip needed
        except Exception:
            return
        x = self.tree.winfo_rootx() + event.x + 15
        y = self.tree.winfo_rooty() + event.y + 20
        self._tip = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=text, bg="#2d2d2d", fg="#f0f0f0",
                       font=("Consolas", 9), padx=8, pady=5, relief="solid", bd=1,
                       wraplength=500, justify="left")
        lbl.pack()
        # Keep tooltip within screen bounds
        tw.update_idletasks()
        sw = tw.winfo_screenwidth()
        sh = tw.winfo_screenheight()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        if x + tw_w > sw - 10:
            x = sw - tw_w - 10
        if y + tw_h > sh - 10:
            y = event.y_root - tw_h - 5
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._after_id:
            self.tree.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None
        self._last_cell = (None, None)


# ── Theme setup ──────────────────────────────────────────────────────────
def setup_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
    default_font = (font_family, 9)
    bold_font = (font_family, 9, "bold")
    small_font = (font_family, 8)

    style.configure("TFrame", background=C["bg"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=default_font)
    style.configure("M.TLabel", background=C["bg"], foreground=C["text2"], font=small_font)
    style.configure("B.TLabel", background=C["bg"], foreground=C["text"], font=bold_font)
    style.configure("H.TFrame", background=C["hbg"])
    style.configure("H.TLabel", background=C["hbg"], foreground=C["hfg"], font=(font_family, 14, "bold"))
    style.configure("HI.TLabel", background=C["hbg"], foreground=C["hfg"], font=default_font)
    style.configure("S.TFrame", background=C["sbg"])
    style.configure("S.TLabel", background=C["sbg"], foreground=C["text"], font=default_font)

    style.configure("TButton", font=default_font, padding=(8, 3))
    style.configure("P.TButton", background=C["accent"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("P.TButton", background=[("active", "#003d99")])
    style.configure("D.TButton", background=C["red"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("D.TButton", background=[("active", "#b52a00")])
    style.configure("G.TButton", background=C["green"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("G.TButton", background=[("active", "#006644")])
    style.configure("Sm.TButton", font=small_font, padding=(4, 1))
    style.configure("HB.TButton", background="#0052cc", foreground="#ffffff", font=default_font, padding=(8, 3))
    style.map("HB.TButton", background=[("active", "#003d99")])

    style.configure("Treeview", background=C["bg"], fieldbackground=C["bg"],
                     foreground=C["text"], rowheight=24, font=default_font,
                     borderwidth=0)
    style.map("Treeview", background=[("selected", C["tsel"])],
              foreground=[("selected", C["text"])])
    style.configure("Treeview.Heading", background=C["bg3"], foreground=C["text"],
                     font=bold_font, relief="flat")

    style.configure("Sc.Treeview", background=C["sbg"], fieldbackground=C["sbg"],
                     foreground=C["text"], rowheight=24, font=default_font)
    style.configure("Sc.Treeview.Heading", background=C["bg3"], foreground=C["text"],
                     font=bold_font, relief="flat")
    style.map("Sc.Treeview", background=[("selected", C["tsel"])])
    style.configure("Treeview.Heading", relief="solid", borderwidth=1)

    style.configure("TCombobox", font=default_font)
    style.configure("TEntry", font=default_font)
    style.configure("SE.TEntry", font=(font_family, 11))
    style.configure("TProgressbar", troughcolor=C["bg3"], background=C["accent"])
    style.configure("TCheckbutton", background=C["bg"], font=default_font)
