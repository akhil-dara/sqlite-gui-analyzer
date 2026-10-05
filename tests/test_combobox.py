"""The searchable dropdown (combobox.SearchableCombobox): the matcher, the ttk.Combobox API it
replaces, opening, filtering, keyboard, recent choices, groups, a 50,000-value list, destroy."""

import time
import tkinter as tk
import unittest
from tkinter import ttk

from tests.helpers import focused, off_screen_windows, send_key, tk_root, within
import combobox
from combobox import SearchableCombobox, filter_items, match
from engine import limits


class MatchTest(unittest.TestCase):
    def test_ranks_and_positions(self):
        self.assertEqual(match("msgstore.db", "MsgStore.db"), (0, list(range(11))))
        self.assertEqual(match("msg", "msgstore.db"), (1, [0, 1, 2]))
        self.assertEqual(match("store", "msgstore.db"), (2, [3, 4, 5, 6, 7]))
        # the occurrence at the start of a word is the one highlighted
        self.assertEqual(match("ab", "xab ab"), (2, [4, 5]))
        self.assertEqual(match("md", "message_date"), (3, [0, 8]))
        self.assertEqual(match("msg db", "msgstore.db"), (3, [0, 1, 2, 9, 10]))
        self.assertEqual(match("rowId", "chat_row_id")[0], 3)
        self.assertEqual(match("msgdb", "msgstore.db"), (3, [0, 1, 2, 9, 10]))
        self.assertEqual(match("mgs", "messages"), (4, [0, 5, 7]))
        self.assertIsNone(match("zz", "msgstore.db"))
        self.assertIsNone(match("dbm", "msgstore.db"))
        self.assertEqual(match("", "anything"), (0, []))
        self.assertEqual(match("1", 12)[0], 1)              # any object, by str()

    def test_filter_order(self):
        items = ["xmsgx", "a_msg", "msgstore.db", "MSG", "m_s_g", "other", "mxsxg"]
        got = [i for i, _p in filter_items("msg", items)]
        self.assertEqual(got, ["MSG", "msgstore.db", "xmsgx", "a_msg", "m_s_g", "mxsxg"])
        # the original order within a rank; empty query: everything
        self.assertEqual([i for i, _ in filter_items("a", ["ba", "ca", "da"])],
                         ["ba", "ca", "da"])
        self.assertEqual(filter_items("  ", [1, 2]), [(1, []), (2, [])])
        self.assertEqual(filter_items("b", [{"k": 1}, "b"], key=str), [("b", [0])])


class ComboTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        off_screen_windows(self)
        self.host = tk.Toplevel(self.root)       # a mapped window (off the screen) for focus
        combobox.set_recent_store(None, None)
        self.addCleanup(combobox.set_recent_store, None, None)
        self.selected = []

    def make(self, **kw):
        c = SearchableCombobox(self.host, **kw)
        c.pack(padx=10, pady=10)
        c.bind("<<ComboboxSelected>>", lambda e: self.selected.append(c.get()))
        self.pump()
        c.entry.focus_set()
        self.pump()
        return c

    def pump(self):
        for _ in range(3):
            self.root.update()

    # Keys are delivered through the widget's bindings (helpers.send_key): event_generate
    # needs the application to hold the system's keyboard focus, which another program (a
    # second test run) can take at any moment.
    def key(self, widget, keysym, modifiers=()):
        send_key(widget, keysym, modifiers=modifiers)
        self.pump()

    def type(self, widget, text):
        for ch in text:
            send_key(widget, ch if ch.isalnum() else
                     {" ": "space", ".": "period", "_": "underscore"}[ch], ch)
        self.pump()

    def click_entry(self, c):
        c.entry.event_generate("<Button-1>", x=5, y=5)
        self.pump()

    def popups(self):
        return [w for w in _all_children(self.root) if isinstance(w, tk.Toplevel)
                and w is not self.host]


def _all_children(w):
    out = []
    for ch in w.winfo_children():
        out.append(ch)
        out.extend(_all_children(ch))
    return out


class ApiTest(ComboTestBase):
    def test_drop_in_options(self):
        var = tk.StringVar(value="b")
        c = self.make(textvariable=var, values=["a", "b", "c"], state="readonly", width=28)
        self.assertEqual(c.winfo_class(), "TFrame")
        self.assertEqual(c["values"], ("a", "b", "c"))
        self.assertEqual(c.cget("values"), ("a", "b", "c"))
        self.assertEqual(c["state"], "readonly")
        self.assertEqual(c.cget("width"), 28)
        self.assertEqual(str(c.entry.cget("state")), "readonly")
        c.configure(values=["x", "y"], width=40)
        self.assertEqual(c["values"], ("x", "y"))
        self.assertEqual(c["width"], 40)
        c["values"] = ("p", "q", "r")
        self.assertEqual(c.cget("values"), ("p", "q", "r"))
        c.config(state="disabled")
        self.assertEqual(str(c.entry.cget("state")), "disabled")
        c.configure({"state": "normal"})
        self.assertEqual(c["state"], "normal")
        c.configure(padding=3)                                  # a Frame option
        self.assertIn("values", c.keys())
        self.assertIn("padding", c.keys())
        self.assertEqual(c.configure("height")[-1], 12)
        with self.assertRaises(tk.TclError):
            c.configure(state="bogus")

    def test_get_set_current_and_variable_both_ways(self):
        var = tk.StringVar(value="b")
        c = self.make(textvariable=var, values=["a", "b", "c"], state="readonly")
        self.assertEqual(c.get(), "b")
        self.assertEqual(c.entry.get(), "b")
        self.assertEqual(c.current(), 1)
        c.set("c")
        self.assertEqual(var.get(), "c")
        self.assertEqual(c.entry.get(), "c")
        var.set("a")
        self.assertEqual(c.get(), "a")
        self.assertEqual(c.entry.get(), "a")
        c.current(2)
        self.assertEqual(var.get(), "c")
        c.set("not listed")
        self.assertEqual(c.current(), -1)
        with self.assertRaises(tk.TclError):
            c.current(5)
        other = tk.StringVar(value="b")
        c.configure(textvariable=other)
        self.assertEqual(c.entry.get(), "b")
        var.set("a")                                            # the old one is let go
        self.assertEqual(c.get(), "b")
        self.assertEqual(c.cget("textvariable"), str(other))
        self.assertEqual(self.selected, [])                     # never sent by code

    def test_labels_and_objects(self):
        c = self.make(values=[1, 2, 3], labels={2: "two"}, state="readonly")
        c.set(2)
        self.assertEqual(c.get(), "2")
        self.assertEqual(c.entry.get(), "two")
        self.assertEqual(c.current(), 1)

    def test_postcommand_runs_before_opening(self):
        calls = []

        def post():
            calls.append(c.is_open())
            c.configure(values=["new1", "new2"])
        c = self.make(values=["old"], state="readonly", postcommand=post)
        c.open()
        self.pump()
        self.assertEqual(calls, [False])
        self.assertEqual(c.visible_rows(), [("item", "new1"), ("item", "new2")])
        self.assertEqual(c.footer_text(), "2 items")
        c.close()
        self.assertEqual(c.cget("postcommand"), post)


class CycleArrowsTest(ComboTestBase):
    """DB Browser-style: Up/Down with closed popup cycles values directly."""

    def setUp(self):
        ComboTestBase.setUp(self)
        self.values = ["alpha", "beta", "gamma", "delta"]
        self.c = self.make(values=self.values, state="readonly", cycle_on_arrows=True)
        self.c.set("beta")
        self.pump()

    def test_down_cycles_to_next(self):
        c = self.c
        c.entry.focus_set()
        self.pump()
        self.key(c.entry, "Down")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "gamma")
        self.assertEqual(self.selected, ["gamma"])

    def test_up_cycles_to_previous(self):
        c = self.c
        c.entry.focus_set()
        self.pump()
        self.key(c.entry, "Up")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "alpha")

    def test_wraps_around(self):
        c = self.c
        c.set("delta")
        self.pump()
        c.entry.focus_set()
        self.pump()
        self.key(c.entry, "Down")
        self.assertEqual(c.get(), "alpha")
        self.key(c.entry, "Up")
        self.assertEqual(c.get(), "delta")

    def test_without_flag_down_opens_popup(self):
        c = self.make(values=self.values, state="readonly")
        c.set("beta")
        self.pump()
        c.entry.focus_set()
        self.pump()
        self.key(c.entry, "Down")
        self.assertTrue(c.is_open())


class ReadonlyTest(ComboTestBase):
    def setUp(self):
        ComboTestBase.setUp(self)
        self.values = ["messages", "msgstore.db", "chat_list", "contacts", "media",
                       "message_date"]
        self.c = self.make(values=self.values, state="readonly", height=4)
        self.c.set("chat_list")
        self.pump()

    def test_click_opens_and_shows_the_list(self):
        c = self.c
        self.assertFalse(c.is_open())
        self.click_entry(c)
        self.assertTrue(c.is_open())
        self.assertEqual(len(self.popups()), 1)
        self.assertEqual(c.active_value(), "chat_list")         # the current value
        self.assertEqual(c.footer_text(), "6 items")
        self.assertEqual(focused(c._search), str(c._search))
        self.click_entry(c)                                     # a second click closes it
        self.assertFalse(c.is_open())
        self.pump()
        self.assertEqual(self.popups(), [])

    def test_keys_open_it(self):
        c = self.c
        c.entry.focus_set()
        self.pump()
        for keysym in ("Down", "space"):
            self.key(c.entry, keysym)
            self.assertTrue(c.is_open(), keysym)
            self.key(c._search, "Escape")
            self.assertFalse(c.is_open())
        self.key(c.entry, "Down", ("Alt",))
        self.assertTrue(c.is_open())
        c.close()
        self.pump()
        self.type(c.entry, "m")                 # a character starts the search with it
        self.assertTrue(c.is_open())
        self.assertEqual(c._svar.get(), "m")
        self.assertEqual(c.footer_text(), "4 of 6")

    def test_typing_filters_and_counts(self):
        c = self.c
        self.click_entry(c)
        self.type(c._search, "msg")
        self.assertEqual(c.visible_rows(), [("item", "msgstore.db"), ("item", "messages"),
                                            ("item", "message_date")])
        self.assertEqual(c.footer_text(), "3 of 6")
        self.assertEqual(c.active_value(), "msgstore.db")       # the best match
        self.type(c._search, "qq")
        self.assertEqual(c.visible_rows(), [])
        self.assertEqual(c.footer_text(), "0 of 6")
        texts = [c._canvas.itemcget(i, "text") for i in c._canvas.find_all()
                 if c._canvas.type(i) == "text"]
        self.assertIn("No match for “msgqq”", texts)
        # the matched characters are highlighted (amber rectangles behind them)
        c._svar.set("store")
        self.pump()
        fills = [c._canvas.itemcget(i, "fill") for i in c._canvas.find_all()
                 if c._canvas.type(i) == "rectangle"]
        self.assertIn(combobox.COLOR["highlight"], fills)

    def test_enter_selects_once_and_escape_restores(self):
        c = self.c
        self.click_entry(c)
        self.type(c._search, "cont")
        self.key(c._search, "Return")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "contacts")
        self.assertEqual(self.selected, ["contacts"])
        self.assertEqual(focused(c.entry), str(c.entry))
        self.click_entry(c)
        self.key(c._search, "Down")
        self.key(c._search, "Escape")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "contacts")
        self.assertEqual(self.selected, ["contacts"])

    def test_arrows_page_home_end_and_scrolling(self):
        c = self.c
        self.click_entry(c)
        self.assertEqual(c.active_value(), "chat_list")
        self.key(c._search, "Down")
        self.assertEqual(c.active_value(), "contacts")
        self.key(c._search, "Up")
        self.key(c._search, "Up")
        self.assertEqual(c.active_value(), "msgstore.db")
        self.key(c._search, "End")
        self.assertEqual(c.active_value(), "message_date")
        self.assertEqual(c._top, 2)                             # scrolled into view
        self.key(c._search, "Down")                             # stays at the end
        self.assertEqual(c.active_value(), "message_date")
        self.key(c._search, "Home")
        self.assertEqual(c.active_value(), "messages")
        self.key(c._search, "Next")
        self.assertEqual(c.active_value(), "media")
        self.key(c._search, "Prior")
        self.assertEqual(c.active_value(), "messages")
        c._on_scrollbar("scroll", "1", "units")
        self.assertEqual(c._top, 1)
        c._on_scrollbar("moveto", "1.0")
        self.assertEqual(c._top, 2)

    def test_mouse_hover_and_click_select(self):
        c = self.c
        self.click_entry(c)
        cv = c._canvas
        cv.event_generate("<Motion>", x=20, y=combobox.ROW + 5)
        self.pump()
        self.assertEqual(c.active_value(), "msgstore.db")
        cv.event_generate("<Button-1>", x=20, y=combobox.ROW * 3 + 5)
        self.pump()
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "contacts")
        self.assertEqual(self.selected, ["contacts"])

    def test_tab_selects_and_moves_on(self):
        c = self.c
        after = ttk.Entry(self.host)
        after.pack()
        self.pump()
        self.click_entry(c)
        self.key(c._search, "Down")
        self.key(c._search, "Tab")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "contacts")
        self.assertEqual(self.selected, ["contacts"])
        self.assertEqual(focused(after), str(after))

    def test_focus_elsewhere_or_a_click_outside_closes(self):
        c = self.c
        other = ttk.Entry(self.host)
        other.pack()
        self.pump()
        self.click_entry(c)
        # the focus going to another widget of the app (what Tk reports with FocusOut; the
        # widget holding it is given here, whichever program has the system's focus)
        other.focus_set()
        c._focus_now = lambda: other
        c._on_focus_out()
        self.pump()
        self.assertFalse(c.is_open())
        del c._focus_now
        # another program taking the keyboard focus: the list stays open
        self.click_entry(c)
        self.assertTrue(c.is_open())
        c._focus_now = lambda: None
        c._on_focus_out()
        self.pump()
        self.assertTrue(c.is_open())
        del c._focus_now
        c.close()
        self.pump()
        self.click_entry(c)
        self.assertTrue(c.is_open())
        lbl = ttk.Label(self.host, text="x")
        lbl.pack()
        self.pump()
        lbl.event_generate("<ButtonPress-1>", x=1, y=1)
        self.pump()
        self.assertFalse(c.is_open())
        self.assertNotIn("%W", str(self.root.tk.call("bind", "all", "<ButtonPress>")))
        self.assertEqual(self.selected, [])


class GroupsTest(ComboTestBase):
    def test_headers_are_skipped_and_counts_shown(self):
        c = self.make(groups=[("Tables", ["messages", "chats"]), ("Views", ["v_all"])],
                      counts={"messages": 1200, "chats": 3}, state="readonly")
        self.assertEqual(c["values"], ("messages", "chats", "v_all"))
        c.open()
        self.pump()
        self.assertEqual(c.visible_rows(), [("header", "Tables"), ("item", "messages"),
                                            ("item", "chats"), ("header", "Views"),
                                            ("item", "v_all")])
        self.assertEqual(c.active_value(), "messages")
        self.key(c._search, "Down")
        self.key(c._search, "Down")                 # over the 'Views' header
        self.assertEqual(c.active_value(), "v_all")
        self.key(c._search, "Up")
        self.assertEqual(c.active_value(), "chats")
        self.key(c._search, "Home")
        self.key(c._search, "Up")                   # never onto the first header
        self.assertEqual(c.active_value(), "messages")
        texts = [c._canvas.itemcget(i, "text") for i in c._canvas.find_all()
                 if c._canvas.type(i) == "text"]
        self.assertIn("1,200", texts)
        self.assertIn("Tables", texts)
        self.type(c._search, "v")
        self.assertEqual(c.visible_rows(), [("header", "Views"), ("item", "v_all")])
        self.assertEqual(c.footer_text(), "1 of 3")
        # clicking a header does nothing
        c._svar.set("")
        self.pump()
        c._canvas.event_generate("<Button-1>", x=20, y=5)
        self.pump()
        self.assertTrue(c.is_open())
        self.assertEqual(self.selected, [])


class NormalModeTest(ComboTestBase):
    def test_typing_autocompletes_enter_escape(self):
        c = self.make(values=["alpha", "beta", "alphabet", "gamma"], state="normal")
        c.entry.focus_set()
        self.pump()
        self.type(c.entry, "alp")
        self.assertTrue(c.is_open())
        self.assertEqual(c.get(), "alp")
        self.assertEqual(c.visible_rows(), [("item", "alpha"), ("item", "alphabet")])
        self.assertEqual(c.footer_text(), "2 of 4")
        self.assertIsNone(c.active_value())                 # the typed text is kept
        self.key(c.entry, "Down")
        self.key(c.entry, "Down")
        self.assertEqual(c.active_value(), "alphabet")
        self.key(c.entry, "Return")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "alphabet")
        self.assertEqual(self.selected, ["alphabet"])
        # free text: Enter keeps it and sends nothing
        c.entry.delete(0, "end")
        self.type(c.entry, "zeta")
        self.assertEqual(c.visible_rows(), [])
        self.key(c.entry, "Return")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "zeta")
        self.assertEqual(self.selected, ["alphabet"])
        # Escape puts back the text there was when the list opened
        self.type(c.entry, "x")
        self.assertTrue(c.is_open())
        self.assertEqual(c.get(), "zetax")
        self.key(c.entry, "Escape")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "zeta")
        self.assertEqual(c.entry.get(), "zeta")

    def test_disabled_ignores_input(self):
        c = self.make(values=["a", "b"], state="disabled")
        c.set("a")
        self.click_entry(c)
        self.assertFalse(c.is_open())
        c._arrow.event_generate("<Button-1>", x=4, y=4)
        self.pump()
        c.open()
        self.assertFalse(c.is_open())
        self.key(c.entry, "Down")
        self.assertFalse(c.is_open())
        self.assertEqual(c.get(), "a")
        c.configure(state="readonly")
        c._arrow.event_generate("<Button-1>", x=4, y=4)
        self.pump()
        self.assertTrue(c.is_open())
        c.configure(state="disabled")                           # closes it
        self.assertFalse(c.is_open())

    def test_placeholder(self):
        c = self.make(values=["a"], state="readonly", placeholder="Pick a table")
        self.assertTrue(c.placeholder_visible())
        c.set("a")
        self.pump()
        self.assertFalse(c.placeholder_visible())


class RecentTest(ComboTestBase):
    def test_recent_first_capped_and_stored(self):
        saved = []
        combobox.set_recent_store(lambda: {"k": ["v3"]}, saved.append)
        values = ["v%d" % i for i in range(20)]
        c = self.make(values=values, state="readonly", recent_key="k")
        c.open()
        self.pump()
        rows = c.visible_rows()
        self.assertEqual(rows[:3], [("header", "Recent"), ("item", "v3"), ("header", "All")])
        self.assertEqual(len(rows), 23)                          # all values still listed
        c.close()
        for i in range(12):                                     # more than the limit
            c.open()
            self.pump()
            c._svar.set("v%d" % (i + 5))
            self.pump()
            self.key(c._search, "Return")
        cap = limits.get("dropdown_recent")
        self.assertEqual(cap, 8)
        c.open()
        self.pump()
        rows = c.visible_rows()
        recent = rows[1:rows.index(("header", "All"))]
        self.assertEqual(len(recent), cap)
        self.assertEqual(recent[0], ("item", "v16"))            # the newest first
        self.assertEqual(len(saved[-1]["k"]), cap)
        c._svar.set("v1")                                       # a search: no Recent part
        self.pump()
        self.assertNotIn(("header", "Recent"), c.visible_rows())


class LargeListTest(ComboTestBase):
    def test_fifty_thousand_values_open_at_once(self):
        values = ["row_%05d_value" % i for i in range(50000)]
        c = self.make(values=values, state="readonly", height=12)
        c.set("row_40000_value")
        t0 = time.perf_counter()
        c.open()
        self.pump()
        elapsed = time.perf_counter() - t0
        within(self, elapsed, 0.5)
        self.assertEqual(c.footer_text(), "50,000 items")
        self.assertEqual(c.active_value(), "row_40000_value")
        self.assertLess(len(c._canvas.find_all()), 100)         # only the rows in view
        self.key(c._search, "End")
        self.assertEqual(c.active_value(), "row_49999_value")
        self.assertLess(len(c._canvas.find_all()), 100)
        t0 = time.perf_counter()
        c._svar.set("4999")
        self.pump()
        within(self, time.perf_counter() - t0, 2.0)
        self.assertTrue(c.footer_text().endswith(" of 50,000"), c.footer_text())
        self.assertFalse(c.footer_text().startswith("0 "))
        rows = c.visible_rows()
        self.assertEqual(rows[0], ("item", "row_04999_value"))  # the substrings first
        self.assertEqual(rows[14], ("item", "row_49999_value"))
        self.assertEqual(c.active_value(), "row_04999_value")
        self.assertLess(len(c._canvas.find_all()), 150)


class DestroyTest(ComboTestBase):
    def test_destroy_while_open(self):
        c = self.make(values=["a", "b"], state="readonly")
        c.open()
        self.pump()
        self.assertEqual(len(self.popups()), 1)
        c.destroy()
        self.pump()
        self.assertEqual(self.popups(), [])
        self.assertNotIn("%W", str(self.root.tk.call("bind", "all", "<ButtonPress>")))
        self.assertNotIn("SearchableCombo", " ".join(self.host.bindtags()))
        c._on_list_click(None)                  # a late event does nothing
        c._on_focus_out()

    def test_host_moving_closes(self):
        c = self.make(values=["a", "b"], state="readonly")
        c.open()
        self.pump()
        self.host.event_generate("<Unmap>")
        self.pump()
        self.assertFalse(c.is_open())


if __name__ == "__main__":
    unittest.main()
