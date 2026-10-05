"""The design system: every tab and window takes its fonts and colours from tokens.py (no
literal font family or colour elsewhere in the UI modules), text colours read at 4.5:1 or
more on the surfaces they are used on, every dropdown is the searchable one, and the theme
builds every style the modules ask for."""

import os
import re
import unittest

from tests.helpers import SRC, tk_root

UI_EXEMPT = ("tokens.py", "constants.py")
FONT_RE = re.compile(r"""["'](Segoe UI|Consolas|Helvetica|Courier|Arial|Tahoma)["']""")
COLOUR_RE = re.compile(r"""["']#[0-9a-fA-F]{6}["']""")


def ui_modules():
    for name in sorted(os.listdir(SRC)):
        if name.endswith(".py") and name not in UI_EXEMPT:
            with open(os.path.join(SRC, name), encoding="utf-8") as f:
                yield name, f.read()


class TokensTest(unittest.TestCase):
    def test_no_literal_fonts_or_colours_outside_the_tokens(self):
        found = []
        for name, text in ui_modules():
            for i, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0] if not COLOUR_RE.search(line) else line
                if FONT_RE.search(code) or COLOUR_RE.search(line):
                    found.append("%s:%d: %s" % (name, i, line.strip()[:90]))
        self.assertEqual(found, [], "\n".join(found))

    def test_every_dropdown_is_searchable(self):
        plain = [name for name, text in ui_modules()
                 if name != "combobox.py" and re.search(r"ttk\.Combobox\s*\(", text)]
        self.assertEqual(plain, [])

    def test_text_contrast(self):
        from tokens import COLOR as K, contrast
        pairs = [("text", "background"), ("text", "card"), ("heading", "card"),
                 ("heading", "background"), ("muted_text", "background"),
                 ("muted_text", "card"), ("primary", "card"), ("primary", "background"),
                 ("on_primary", "primary"), ("danger_text", "background"),
                 ("success_text", "background"), ("warning", "background"),
                 ("text", "selection"), ("text", "accent_soft"), ("heading", "primary_soft"),
                 ("tip_text", "tip"), ("placeholder", "card"), ("white", "danger"),
                 ("text", "muted"), ("heading", "muted")]
        low = [(fg, bg, round(contrast(K[fg], K[bg]), 2)) for fg, bg in pairs
               if contrast(K[fg], K[bg]) < 4.5]
        self.assertEqual(low, [])

    def _check_text_styles(self, root, style):
        from tokens import contrast

        def colour(v):
            v = str(v)
            if v.startswith("#") and len(v) == 7:
                return v
            r, g, b = root.winfo_rgb(v)
            return "#%02x%02x%02x" % (r // 256, g // 256, b // 256)

        def need(font):
            f = str(font)
            return 3.0 if ("bold" in f or "Semibold" in f or any(
                str(n) in f.split() for n in (12, 14, 15))) else 4.5
        names = ["TLabel", "Card.TLabel", "Muted.TLabel", "CardMuted.TLabel", "Small.TLabel",
                 "Heading.TLabel", "CardHeading.TLabel", "Title.TLabel", "Metric.TLabel",
                 "Link.TLabel", "Danger.TLabel", "Success.TLabel", "Warning.TLabel",
                 "Header.TLabel", "HeaderMuted.TLabel", "HeaderTitle.TLabel", "TButton",
                 "Primary.TButton", "Subtle.TButton", "Danger.TButton", "Success.TButton",
                 "Small.TButton", "Header.TButton", "TCheckbutton", "TMenubutton",
                 "TNotebook.Tab", "Treeview", "Treeview.Heading", "Nav.Treeview"]
        low = []
        for n in names:
            fg, bg = style.lookup(n, "foreground"), style.lookup(n, "background")
            if not fg or not bg:
                continue
            r = contrast(colour(fg), colour(bg))
            if r < need(style.lookup(n, "font")):
                low.append((n, fg, bg, round(r, 2)))
            for state in (() if n.endswith("TLabel") else ("active", "pressed", "selected")):
                sfg = style.lookup(n, "foreground", [state]) or fg
                sbg = style.lookup(n, "background", [state]) or bg
                font = style.lookup(n, "font", [state]) or style.lookup(n, "font")
                r = contrast(colour(sfg), colour(sbg))
                if r < need(font):
                    low.append((n, state, sfg, sbg, round(r, 2)))
            dfg = style.lookup(n, "foreground", ["disabled"])
            if dfg and n.endswith("TButton"):
                self.assertNotEqual(colour(dfg), colour(fg), n)
        self.assertEqual(low, [])

    def test_every_text_style_reads_at_4_5_to_1(self):
        """The foreground of every text-bearing style on its own background (and the
        selected tab, the pressed and hovered buttons) reads at 4.5:1 or more; 3:1 for bold
        or large text. Disabled controls are exempt, but still differ from enabled ones."""
        import theme
        root = tk_root(self)
        style = theme.apply(root)
        self._check_text_styles(root, style)

    def test_dark_theme_contrast(self):
        """The dark palette reads as well as the light one: the same text pairs at 4.5:1
        or more, and switching themes is a clean round-trip."""
        import tokens
        from tokens import DARK_COLOR, LIGHT_COLOR, contrast, set_theme, current_theme
        pairs = [("text", "background"), ("text", "card"), ("heading", "card"),
                 ("heading", "background"), ("muted_text", "background"),
                 ("muted_text", "card"), ("primary", "card"), ("primary", "background"),
                 ("on_primary", "primary"), ("danger_text", "background"),
                 ("success_text", "background"), ("warning", "background"),
                 ("text", "selection"), ("text", "accent_soft"),
                 ("heading", "primary_soft"), ("tip_text", "tip"),
                 ("placeholder", "card"), ("text", "muted"), ("heading", "muted"),
                 ("text", "highlight"), ("text", "row_alt"), ("text", "danger_soft"),
                 ("danger_text", "danger_soft"), ("success_text", "success_soft"),
                 ("text", "find_all")]
        low = [(fg, bg, round(contrast(DARK_COLOR[fg], DARK_COLOR[bg]), 2))
               for fg, bg in pairs if contrast(DARK_COLOR[fg], DARK_COLOR[bg]) < 4.5]
        self.assertEqual(low, [])
        self.assertEqual(set(DARK_COLOR), set(LIGHT_COLOR))   # the same token names
        from constants import refresh_colors    # the cleanup below needs it, even on a skip
        self.assertEqual(set_theme("dark"), "dark")
        self.assertEqual(current_theme(), "dark")
        try:
            # the danger button keeps its own deep-red background (white reads on it):
            # the danger token itself stays bright for error text on dark surfaces
            self.assertGreaterEqual(contrast("#ffffff", tokens.BUTTONS["danger"][0]), 4.5)
            # every ttk style reads in the dark theme too
            import theme
            root = tk_root(self)
            self._check_text_styles(root, theme.apply(root))
            from constants import C, refresh_colors
            refresh_colors()
            self.assertEqual(C["bg"], DARK_COLOR["card"])
            self.assertEqual(C["red"], DARK_COLOR["danger"])
        finally:
            self.assertEqual(set_theme("light"), "light")
            refresh_colors()
        self.assertEqual(current_theme(), "light")

    def test_contrast_function(self):
        from tokens import contrast
        self.assertAlmostEqual(contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(contrast("#777777", "#777777"), 1.0, places=3)

    def test_theme_builds_every_style_used(self):
        """Each style name the modules pass exists once the theme is applied."""
        import theme
        from tkinter import ttk
        root = tk_root(self)
        style = theme.apply(root)
        used = set()
        for _name, text in ui_modules():
            used.update(re.findall(r"""style=["']([A-Za-z.]+\.T[A-Za-z]+)["']""", text))
        missing = [s for s in sorted(used)
                   if not style.lookup(s, "background") and not style.lookup(s, "font")
                   and not style.lookup(s, "foreground")]
        self.assertEqual(missing, [])
        self.assertTrue(ttk.Style(root).lookup("Primary.TButton", "background"))


if __name__ == "__main__":
    unittest.main()
