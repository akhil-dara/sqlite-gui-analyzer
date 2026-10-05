"""The ttk styles of the app, built from the design tokens (tokens.py) on the 'clam' theme.

apply(root) is called once per Tk interpreter (widgets.setup_theme). Style names:

  frames    TFrame (window background), Card.TFrame (white, thin border), Panel.TFrame,
            Toolbar.TFrame, Header.TFrame, Popover.TFrame (white, border)
  labels    TLabel, Card.TLabel, Muted.TLabel, CardMuted.TLabel, Small.TLabel,
            CardSmall.TLabel, Heading.TLabel, CardHeading.TLabel, Title.TLabel,
            Metric.TLabel, Link.TLabel, CardLink.TLabel, Danger.TLabel, Success.TLabel,
            Warning.TLabel, Header.TLabel, HeaderMuted.TLabel
  buttons   TButton (secondary), Primary.TButton, Subtle.TButton, Danger.TButton,
            Success.TButton, Small.TButton, Icon.TButton, Link.TButton, Header.TButton
            (older names kept as aliases: P. D. G. Sm. HB.)
  inputs    TEntry, Search.TEntry, TCombobox, TSpinbox, TCheckbutton, Card.TCheckbutton,
            TRadiobutton, TMenubutton, Subtle.TMenubutton
  lists     Treeview, Nav.Treeview (side panel), Treeview.Heading
  other     TNotebook(.Tab), TProgressbar, TScrollbar, TSeparator, TPanedwindow

Every control shows a visible focus ring (the primary colour) when it has the keyboard focus.
"""

from tkinter import ttk

from tokens import COLOR as K, FONT as F, BUTTONS, XS, S

# the style names older code uses, and the ones they stand for now
ALIASES = {"P.TButton": "Primary.TButton", "D.TButton": "Danger.TButton",
           "G.TButton": "Success.TButton", "Sm.TButton": "Small.TButton",
           "HB.TButton": "Header.TButton", "M.TLabel": "Muted.TLabel",
           "B.TLabel": "Heading.TLabel", "S.TLabel": "Panel.TLabel",
           "S.TFrame": "Panel.TFrame", "H.TFrame": "Header.TFrame",
           "H.TLabel": "HeaderTitle.TLabel", "HI.TLabel": "Header.TLabel",
           "Case.TFrame": "Panel.TFrame", "SE.TEntry": "Search.TEntry",
           "Sc.Treeview": "Nav.Treeview"}


def _button(style, name, kind, font=None, padding=(10, 3)):
    font = font if font is not None else F["body"]      # read now: the size can change
    bg, hover, fg = BUTTONS[kind]
    border = K["button_border"] if kind == "secondary" else \
        K["border"] if kind == "subtle" else bg
    style.configure(name, background=bg, foreground=fg, font=font, padding=padding,
                    bordercolor=border, lightcolor=bg, darkcolor=bg, relief="flat",
                    focuscolor=K["ring"], focusthickness=1, anchor="center")
    style.map(name,
              background=[("disabled", K["disabled"]), ("pressed", K["pressed"] if kind in (
                  "secondary", "subtle") else hover), ("active", hover)],
              foreground=[("disabled", K["disabled_text"])],
              bordercolor=[("focus", K["ring"]), ("active", K["pressed"] if kind in (
                  "secondary", "subtle") else hover)],
              lightcolor=[("active", hover)], darkcolor=[("active", hover)])


def row_height(root, extra=8):
    """A list row's height from the body font's line height, so rows grow with the display
    scaling (125-200%) instead of cutting the text."""
    import tkinter.font as tkfont
    try:
        ls = tkfont.Font(root=root, font=F["body"]).metrics("linespace")
    except Exception:                   # noqa: BLE001 - no font system yet
        ls = 15
    return max(20, int(ls) + extra)


def apply(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    rh = row_height(root)
    style.configure(".", background=K["background"], foreground=K["text"], font=F["body"],
                    bordercolor=K["border"], lightcolor=K["card"], darkcolor=K["border"],
                    troughcolor=K["muted"], focuscolor=K["ring"], selectbackground=K["selection"],
                    selectforeground=K["text"], fieldbackground=K["card"],
                    insertcolor=K["text"])

    # frames
    style.configure("TFrame", background=K["background"])
    style.configure("Panel.TFrame", background=K["background"])
    style.configure("Toolbar.TFrame", background=K["background"])
    style.configure("Header.TFrame", background=K["card"])
    style.configure("Card.TFrame", background=K["card"], bordercolor=K["border"],
                    relief="solid", borderwidth=1)
    style.configure("Popover.TFrame", background=K["card"], bordercolor=K["border"],
                    relief="solid", borderwidth=1)
    style.configure("Plain.TFrame", background=K["card"])

    # labels
    for name, bg, fg, font in (
            ("TLabel", K["background"], K["text"], F["body"]),
            ("Panel.TLabel", K["background"], K["text"], F["body"]),
            ("Card.TLabel", K["card"], K["text"], F["body"]),
            ("Muted.TLabel", K["background"], K["muted_text"], F["small"]),
            ("CardMuted.TLabel", K["card"], K["muted_text"], F["small"]),
            ("Small.TLabel", K["background"], K["muted_text"], F["small"]),
            ("CardSmall.TLabel", K["card"], K["muted_text"], F["small"]),
            ("Heading.TLabel", K["background"], K["heading"], F["heading"]),
            ("CardHeading.TLabel", K["card"], K["heading"], F["heading"]),
            ("Title.TLabel", K["background"], K["heading"], F["title"]),
            ("CardTitle.TLabel", K["card"], K["heading"], F["title"]),
            ("Metric.TLabel", K["card"], K["heading"], F["display"]),
            ("Link.TLabel", K["background"], K["primary"], F["small"]),
            ("CardLink.TLabel", K["card"], K["primary"], F["small"]),
            ("Danger.TLabel", K["background"], K["danger_text"], F["small"]),
            ("Success.TLabel", K["background"], K["success_text"], F["small"]),
            ("Warning.TLabel", K["background"], K["warning"], F["small"]),
            ("Header.TLabel", K["card"], K["text"], F["body"]),
            ("HeaderMuted.TLabel", K["card"], K["muted_text"], F["body"]),
            ("HeaderTitle.TLabel", K["card"], K["heading"], F["title"])):
        style.configure(name, background=bg, foreground=fg, font=font)

    # buttons: one primary per area, secondary for the rest, subtle in dense toolbars
    _button(style, "TButton", "secondary")
    _button(style, "Primary.TButton", "primary", F["body_bold"])
    _button(style, "Subtle.TButton", "subtle")
    _button(style, "Danger.TButton", "danger", F["body_bold"])
    _button(style, "Success.TButton", "success", F["body_bold"])
    _button(style, "Small.TButton", "secondary", F["small"], (6, 1))
    _button(style, "Icon.TButton", "subtle", F["body"], (4, 1))
    _button(style, "Header.TButton", "secondary", F["body"], (10, 3))
    style.configure("Link.TButton", background=K["background"], foreground=K["primary"],
                    font=F["small"], padding=(2, 0), relief="flat", borderwidth=0,
                    bordercolor=K["background"], lightcolor=K["background"],
                    darkcolor=K["background"], focuscolor=K["ring"])
    style.map("Link.TButton", foreground=[("active", K["primary_hover"]),
                                          ("disabled", K["disabled_text"])],
              background=[("active", K["background"])])

    # inputs
    for name, font, pad in (("TEntry", F["body"], (6, 3)), ("Search.TEntry", F["large"], (8, 4))):
        style.configure(name, fieldbackground=K["card"], foreground=K["text"], font=font,
                        padding=pad, bordercolor=K["border"], lightcolor=K["card"],
                        darkcolor=K["card"], insertcolor=K["text"])
        style.map(name, bordercolor=[("focus", K["ring"])], lightcolor=[("focus", K["ring"])],
                  fieldbackground=[("readonly", K["background"]), ("disabled", K["muted"])],
                  foreground=[("disabled", K["disabled_text"])])
    style.configure("Invalid.TEntry", fieldbackground=K["danger_soft"], bordercolor=K["danger"],
                    lightcolor=K["danger"], foreground=K["text"], padding=(6, 3))
    style.configure("TCombobox", fieldbackground=K["card"], foreground=K["text"],
                    background=K["card"], arrowcolor=K["muted_text"], padding=(6, 2),
                    bordercolor=K["border"], lightcolor=K["card"], darkcolor=K["card"])
    # the searchable combobox (custom widget): keep its entry readable in both themes
    style.configure("SearchableCombo.TEntry", fieldbackground=K["card"],
                    foreground=K["text"], background=K["card"], insertcolor=K["text"],
                    padding=(6, 3, 24, 3))
    # its state colours too: the widget mapped them once with the theme of the moment, so a
    # switch to dark left a white field with light text (unreadable)
    style.map("SearchableCombo.TEntry",
              fieldbackground=[("disabled", K["muted"]), ("readonly", K["card"])],
              foreground=[("disabled", K["disabled_text"])],
              selectbackground=[("readonly", K["selection"])],
              selectforeground=[("readonly", K["text"])])
    style.map("TCombobox", bordercolor=[("focus", K["ring"])], lightcolor=[("focus", K["ring"])],
              fieldbackground=[("readonly", K["card"])],
              selectbackground=[("readonly", K["card"])],
              selectforeground=[("readonly", K["text"])])
    style.configure("TSpinbox", fieldbackground=K["card"], foreground=K["text"],
                    arrowcolor=K["muted_text"], padding=(4, 2), bordercolor=K["border"],
                    lightcolor=K["card"], darkcolor=K["card"], background=K["card"])
    style.map("TSpinbox", bordercolor=[("focus", K["ring"])], lightcolor=[("focus", K["ring"])])
    # a segmented control: radio buttons drawn as joined buttons, the chosen one filled with
    # the primary colour (which view is on is always visible, not only a small dot)
    try:
        style.layout("Segment.TRadiobutton", style.layout("Toolbutton"))
    except Exception:                   # noqa: BLE001 - a theme without Toolbutton
        pass
    style.configure("Segment.TRadiobutton", background=K["button"], foreground=K["text"],
                    font=F["body"], padding=(10, 3), anchor="center", relief="solid",
                    borderwidth=1, bordercolor=K["button_border"], focuscolor=K["ring"])
    style.map("Segment.TRadiobutton",
              background=[("selected", K["primary"]), ("active", K["hover"])],
              foreground=[("selected", K["on_primary"]), ("disabled", K["disabled_text"])],
              bordercolor=[("selected", K["primary"])],
              relief=[("selected", "solid")])
    for name, bg in (("TCheckbutton", K["background"]), ("Card.TCheckbutton", K["card"]),
                     ("TRadiobutton", K["background"]), ("Card.TRadiobutton", K["card"])):
        style.configure(name, background=bg, foreground=K["text"], font=F["body"],
                        indicatorbackground=K["card"], indicatorforeground=K["primary"],
                        indicatormargin=(0, 0, XS, 0), focuscolor=K["ring"],
                        bordercolor=K["border"], upperbordercolor=K["border"],
                        lowerbordercolor=K["border"])
        style.map(name, background=[("active", bg)],
                  indicatorbackground=[("pressed", K["muted"]), ("selected", K["card"])],
                  foreground=[("disabled", K["disabled_text"])])
    style.configure("TMenubutton", background=K["button"], foreground=K["text"],
                    font=F["body"], padding=(8, 3), bordercolor=K["button_border"],
                    lightcolor=K["button"], darkcolor=K["button"], arrowcolor=K["text"],
                    relief="flat", focuscolor=K["ring"])
    style.map("TMenubutton", background=[("disabled", K["disabled"]), ("active", K["hover"])],
              foreground=[("disabled", K["disabled_text"])],
              bordercolor=[("focus", K["ring"])])
    style.configure("Subtle.TMenubutton", background=K["background"], foreground=K["text"],
                    padding=(6, 2), bordercolor=K["background"], lightcolor=K["background"],
                    darkcolor=K["background"], arrowcolor=K["muted_text"])
    style.map("Subtle.TMenubutton", background=[("active", K["hover"])],
              bordercolor=[("focus", K["ring"])])

    # lists
    style.configure("Treeview", background=K["card"], fieldbackground=K["card"],
                    foreground=K["text"], rowheight=rh, font=F["body"], borderwidth=0,
                    bordercolor=K["border"], lightcolor=K["card"], darkcolor=K["card"])
    style.map("Treeview", background=[("selected", K["selection"])],
              foreground=[("selected", K["text"])])
    style.configure("Treeview.Heading", background=K["muted"], foreground=K["heading"],
                    font=F["small_bold"], relief="flat", borderwidth=1, padding=(6, 3),
                    bordercolor=K["border"], lightcolor=K["muted"], darkcolor=K["muted"])
    style.map("Treeview.Heading", background=[("active", K["hover"])])
    style.configure("Nav.Treeview", background=K["background"], fieldbackground=K["background"],
                    foreground=K["text"], rowheight=rh, font=F["body"], borderwidth=0)
    style.map("Nav.Treeview", background=[("selected", K["selection"])],
              foreground=[("selected", K["text"])])
    style.configure("Nav.Treeview.Heading", background=K["muted"], foreground=K["heading"],
                    font=F["small_bold"], relief="flat")

    # notebook: flat tabs, the selected one white with a primary top edge
    style.configure("TNotebook", background=K["background"], borderwidth=0,
                    tabmargins=(S, XS, S, 0), bordercolor=K["border"], lightcolor=K["border"],
                    darkcolor=K["border"])
    # tabs: dark labels on a muted strip; the selected one white, bold, in the primary colour
    # with a primary top edge
    style.configure("TNotebook.Tab", background=K["muted"], foreground=K["text"],
                    font=F["body"], padding=(12, 5), borderwidth=1, bordercolor=K["border"],
                    lightcolor=K["muted"], darkcolor=K["muted"], focuscolor=K["ring"])
    style.map("TNotebook.Tab",
              background=[("selected", K["card"]), ("active", K["hover"])],
              foreground=[("selected", K["primary"]), ("active", K["text"])],
              lightcolor=[("selected", K["primary"])],
              bordercolor=[("selected", K["primary"])],
              font=[("selected", F["body_bold"])],
              expand=[("selected", (0, 2, 0, 0))])

    # other
    style.configure("TProgressbar", troughcolor=K["muted"], background=K["secondary"],
                    bordercolor=K["muted"], lightcolor=K["secondary"], darkcolor=K["secondary"],
                    thickness=6)
    style.configure("TScrollbar", background=K["border"], troughcolor=K["background"],
                    bordercolor=K["background"], lightcolor=K["border"], darkcolor=K["border"],
                    arrowcolor=K["muted_text"], gripcount=0, arrowsize=12)
    style.map("TScrollbar", background=[("active", K["muted_text"]), ("pressed", K["muted_text"])])
    style.configure("TSeparator", background=K["border"])
    style.configure("TPanedwindow", background=K["background"])
    style.configure("Sash", sashthickness=6, gripcount=0, background=K["background"])
    style.configure("TLabelframe", background=K["background"], bordercolor=K["border"])
    style.configure("TLabelframe.Label", background=K["background"], foreground=K["heading"],
                    font=F["small_bold"])

    # the older style names, as the styles they stand for
    for old, new in ALIASES.items():
        _alias(style, old, new)
    return style


def _alias(style, old, new):
    opts = {}
    for key in ("background", "foreground", "font", "padding", "bordercolor", "lightcolor",
                "darkcolor", "relief", "focuscolor", "focusthickness", "fieldbackground",
                "anchor", "rowheight", "borderwidth", "insertcolor"):
        v = style.lookup(new, key)
        if v not in ("", None):
            opts[key] = v
    if opts:
        style.configure(old, **opts)
    m = style.map(new)
    if m:
        style.map(old, **m)
    if new.endswith("Treeview"):
        style.configure(old + ".Heading", **dict(
            (k, style.lookup(new + ".Heading", k)) for k in ("background", "foreground", "font")
            if style.lookup(new + ".Heading", k)))
