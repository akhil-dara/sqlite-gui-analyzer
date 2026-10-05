"""The View menu's Interface size: the choice grows or shrinks every font token (and so the
whole UI, live, in every window already open), is kept in the app settings, and is applied
again at the next startup."""

import os
import shutil
import tempfile
import unittest

from tests.helpers import TempDirTest, free_tk, off_screen_windows


class InterfaceSizeTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_uiscale_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        off_screen_windows(self)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False
        import tokens
        # the font tokens are shared by the whole process: back to Default for later tests
        self.addCleanup(tokens.set_font_scale, 1.0)

    def test_choices_scale_fonts_and_widgets(self):
        import tokens
        from app import _UI_SCALE_FACTORS
        base = tokens.BASE_FONT["body"][1]
        btn = self.app._help_btn
        self.app.update_idletasks()
        heights = {}
        for key, factor in sorted(_UI_SCALE_FACTORS.items(), key=lambda kv: kv[1]):
            self.assertEqual(self.app.set_ui_scale(key), key)
            self.app.update_idletasks()
            self.assertEqual(tokens.FONT["body"][1], max(6, int(round(base * factor))))
            self.assertEqual(self.app._ui_scale_var.get(), key)
            heights[key] = btn.winfo_reqheight()
        # a button already on screen follows the size (Tk's own scaling would not move it)
        self.assertLess(heights["compact"], heights["default"])
        self.assertLess(heights["default"], heights["xlarge"])

    def test_unknown_choice_falls_back_to_default(self):
        import tokens
        self.assertEqual(self.app.set_ui_scale("huge-typo"), "default")
        self.assertEqual(tokens.font_scale(), 1.0)

    def test_choice_is_saved_in_settings(self):
        from engine.tags import save_settings  # noqa: F401  (kept in step with set_theme)
        self.app.set_ui_scale("large")
        self.assertEqual(self.app.tags.settings.get("ui_scale"), "large")

    def test_view_menu_lists_the_sizes(self):
        menu = self.app._view_menu
        idx = menu.index("Interface size")
        self.assertIsNotNone(idx)
        size_menu = menu.nametowidget(menu.entrycget(idx, "menu"))
        labels = [size_menu.entrycget(i, "label") for i in range(size_menu.index("end") + 1)]
        self.assertEqual(labels, ["Small", "Default", "Large", "Extra large"])
        self.app.set_ui_scale("xlarge")
        for i in range(size_menu.index("end") + 1):
            key = ("compact", "default", "large", "xlarge")[i]
            var = size_menu.entrycget(i, "variable")
            self.assertEqual(getattr(var, "string", str(var)), self.app._ui_scale_var._name)
            self.assertEqual(size_menu.entrycget(i, "value"), key)


if __name__ == "__main__":
    unittest.main()
