"""Design tokens: the one place for spacing, type, colours and control sizes.

Every window and tab takes its look from here (widgets.setup_theme builds the ttk styles from
these values; constants.C maps the older colour names onto them), so a colour or a size is
changed once. A clean, dense style for a professional data tool: navy and grey, amber only for
highlights, cards with subtle borders, one font family, visible focus rings. No Tk here: the
values are plain data.

  SPACE / XS S M L XL  spacing steps 4, 8, 12, 16, 24 px
  FONT                 typography: 9 / 10 / 12 / 14 (semibold headings) and mono
  COLOR                surfaces, borders, text, primary / secondary, amber accent, states
                       (the live tokens: LIGHT_COLOR at startup, swapped in place by
                       set_theme("dark"); DARK_COLOR is the dark palette)
  DB_COLORS            one colour per database of a case (engine.case.PALETTE)
  HEIGHT               control, row and toolbar heights
  BUTTONS              the button kinds: primary, secondary, subtle, danger, success

Contrast: every text colour here reads at 4.5:1 or more on the surfaces it is used on
(tests/test_tokens.py checks the pairs).
"""

import sys

from engine.case import PALETTE

if sys.platform == "win32":
    FAMILY, SEMIBOLD, MONO_FAMILY = "Segoe UI", "Segoe UI Semibold", "Consolas"
else:
    FAMILY, SEMIBOLD, MONO_FAMILY = "Helvetica", "Helvetica", "Courier"

XS, S, M, L, XL = 4, 8, 12, 16, 24
SPACE = {"xs": XS, "s": S, "m": M, "l": L, "xl": XL}

FONT = {
    "tiny": (FAMILY, 7),                    # badges, legends
    "tiny_bold": (FAMILY, 7, "bold"),
    "italic": (FAMILY, 9, "italic"),
    "body": (FAMILY, 9),
    "body_bold": (SEMIBOLD, 9),
    "small": (FAMILY, 8),
    "small_bold": (SEMIBOLD, 8),
    "label": (FAMILY, 10),
    "heading": (SEMIBOLD, 10),              # section headers
    "title": (SEMIBOLD, 12),                # the app / case name, window titles
    "display": (SEMIBOLD, 14),              # the number of a summary card
    "large": (FAMILY, 11),                  # the Search tab's term
    "mono": (MONO_FAMILY, 9),
    "mono_small": (MONO_FAMILY, 8),
    "mono_small_bold": (MONO_FAMILY, 8, "bold"),
    "mono_large": (MONO_FAMILY, 10),
    "mono_bold": (MONO_FAMILY, 10, "bold"),
    "mono_italic": (MONO_FAMILY, 10, "italic"),
}
BASE_FONT = dict(FONT)          # the sizes at the Default interface size
_font_scale = 1.0


def font_scale():
    """The interface size factor the FONT tokens hold (1.0: Default)."""
    return _font_scale


def set_font_scale(factor):
    """Grow or shrink every FONT token by factor (View › Interface size), in place, so every
    module holding FONT draws with the new sizes; returns {old font tuple: new font tuple}
    for widgets already made with the old ones."""
    global _font_scale
    _font_scale = float(factor)
    changed = {}
    for key, base in BASE_FONT.items():
        size = max(6, int(round(base[1] * _font_scale)))
        new = (base[0], size) + tuple(base[2:])
        if FONT[key] != new:
            changed[FONT[key]] = new
        FONT[key] = new
    return changed

LIGHT_COLOR = {
    # surfaces
    "background": "#f8fafc",            # the window, side panels
    "card": "#ffffff",                  # cards, lists, entries
    "muted": "#e9eef6",                 # headings, troughs, chips
    "hover": "#e2e8f0",
    "pressed": "#cbd5e1",
    "button": "#f1f5f9",                # a secondary button's solid fill
    "button_border": "#94a3b8",         # and its border: buttons never look disabled
    "disabled": "#e2e8f0",              # a disabled control's fill
    "border": "#cbd5e1",
    "border_soft": "#dbeafe",
    # text
    "text": "#0f172a",
    "heading": "#1e3a8a",
    "muted_text": "#475569",
    "placeholder": "#64748b",
    "disabled_text": "#64748b",
    # brand
    "primary": "#1e40af",
    "primary_hover": "#1e3a8a",
    "on_primary": "#ffffff",
    "secondary": "#3b82f6",
    "primary_soft": "#dbeafe",
    "selection": "#dbeafe",
    "ring": "#1e40af",
    # amber: highlights only (a search match, the current find, a drag range)
    "accent": "#d97706",
    "accent_soft": "#fef3c7",
    "highlight": "#fde68a",
    # states
    "danger": "#dc2626",
    "danger_text": "#b91c1c",
    "danger_hover": "#b91c1c",
    "danger_soft": "#fee2e2",
    "success": "#16a34a",
    "success_text": "#15803d",
    "success_hover": "#15803d",
    "success_soft": "#dcfce7",
    "warning": "#b45309",
    "warning_soft": "#fef3c7",
    "purple": "#6d28d9",
    "purple_soft": "#ede9fe",
    "row_alt": "#f8fafc",
    "find_all": "#fef9c3",              # every match of a find in a text
    "teal": "#0e7490",                  # values looked up from a linked table
    # tooltips
    "tip": "#1e293b",
    "tip_text": "#f8fafc",
    # the app icon
    "logo": "#3b82f6",
    "logo_light": "#60a5fa",
    "logo_dark": "#1e40af",
    "logo_edge": "#1e3a8a",
    "logo_line": "#93c5fd",
    "white": "#ffffff",
}

DARK_COLOR = {
    # surfaces
    "background": "#0f172a",            # the window, side panels (slate-900)
    "card": "#1e293b",                  # cards, lists, entries (slate-800)
    "muted": "#334155",                 # headings, troughs, chips (slate-700)
    "hover": "#3d4f6d",
    "pressed": "#475569",
    "button": "#2b3b55",                # a secondary button's solid fill
    "button_border": "#64748b",         # and its border: buttons never look disabled
    "disabled": "#1e293b",              # a disabled control's fill
    "border": "#3f5371",
    "border_soft": "#1e40af",
    # text
    "text": "#f1f5f9",
    "heading": "#93c5fd",
    "muted_text": "#cbd5e1",
    "placeholder": "#94a3b8",
    "disabled_text": "#64748b",
    # brand
    "primary": "#60a5fa",
    "primary_hover": "#93c5fd",
    "on_primary": "#0f172a",
    "secondary": "#60a5fa",
    "primary_soft": "#1e3a8a",
    "selection": "#1d4ed8",
    "ring": "#93c5fd",
    # amber: highlights only (a search match, the current find, a drag range)
    "accent": "#f59e0b",
    "accent_soft": "#451a03",
    "highlight": "#92400e",
    # states
    "danger": "#ef4444",
    "danger_text": "#fca5a5",
    "danger_hover": "#f87171",
    "danger_soft": "#450a0a",
    "success": "#22c55e",
    "success_text": "#86efac",
    "success_hover": "#4ade80",
    "success_soft": "#052e16",
    "warning": "#fbbf24",
    "warning_soft": "#451a03",
    "purple": "#a78bfa",
    "purple_soft": "#2e1065",
    "row_alt": "#17233d",
    "find_all": "#713f12",              # every match of a find in a text
    "teal": "#2dd4bf",                  # values looked up from a linked table
    # tooltips
    "tip": "#020617",
    "tip_text": "#f8fafc",
    # the app icon
    "logo": "#3b82f6",
    "logo_light": "#60a5fa",
    "logo_dark": "#1e40af",
    "logo_edge": "#1e3a8a",
    "logo_line": "#93c5fd",
    "white": "#ffffff",
}

# The live tokens. Every module does `from tokens import COLOR as K` and reads it when it
# draws, so set_theme() swaps the values in place and the whole app follows (the ttk
# styles are rebuilt too, see widgets.set_app_theme).
COLOR = dict(LIGHT_COLOR)

THEME_NAMES = ("light", "dark")
_theme = "light"


def current_theme():
    """'light' or 'dark': the theme the live COLOR tokens hold."""
    return _theme


def set_theme(name):
    """Switch the live COLOR (and BUTTONS) tokens to 'light' or 'dark', in place, so
    every module holding COLOR sees it. Returns the theme in effect."""
    global _theme
    name = "dark" if name == "dark" else "light"
    _theme = name                     # first: _button_colors() reads it
    src = DARK_COLOR if name == "dark" else LIGHT_COLOR
    COLOR.clear()
    COLOR.update(src)
    BUTTONS.clear()
    BUTTONS.update(_button_colors())
    return name


def _button_colors():
    """(background, hover background, text colour) of the button kinds, from COLOR. In the
    dark theme the danger and success buttons stay deep (white reads 4.8:1 or more on
    every state) while the danger/success tokens themselves stay bright for text on
    dark surfaces."""
    if _theme == "dark":
        return {
            "primary": (COLOR["primary"], COLOR["primary_hover"], COLOR["on_primary"]),
            "secondary": (COLOR["button"], COLOR["hover"], COLOR["text"]),
            "subtle": (COLOR["background"], COLOR["hover"], COLOR["text"]),
            "danger": ("#b91c1c", "#dc2626", COLOR["white"]),
            "success": ("#166534", "#15803d", COLOR["white"]),
        }
    return {
        "primary": (COLOR["primary"], COLOR["primary_hover"], COLOR["on_primary"]),
        "secondary": (COLOR["button"], COLOR["hover"], COLOR["text"]),
        "subtle": (COLOR["background"], COLOR["hover"], COLOR["text"]),
        "danger": (COLOR["danger"], COLOR["danger_hover"], COLOR["white"]),
        "success": (COLOR["success_text"], COLOR["success_hover"], COLOR["white"]),
    }


# colours Tk widgets carry when nothing set them (the theme switch recolours those too)
PLATFORM_DEFAULT_FG = {"systemwindowtext", "systembuttontext", "black", "#000000", "#000",
               "systemmenutext"}
PLATFORM_DEFAULT_BG = {"systemwindow", "systembuttonface", "white", "#ffffff", "#fff", "#f0f0f0",
               "systemmenu", "#d9d9d9"}
PLATFORM_DEFAULT_SELECT = {"systemhighlight", "#0078d7", "#c3c3c3"}

DB_COLORS = PALETTE

HEIGHT = {"control": 26, "row": 24, "toolbar": 34, "header": 44}

# (background, hover background, text colour) of the button kinds
BUTTONS = _button_colors()


def contrast(fg, bg):
    """WCAG contrast ratio of two '#rrggbb' colours (1.0 .. 21.0)."""
    def lum(c):
        c = c.lstrip("#")
        out = []
        for i in (0, 2, 4):
            v = int(c[i:i + 2], 16) / 255.0
            out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)
