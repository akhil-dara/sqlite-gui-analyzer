"""The diagram canvas (erd_view.ErdCanvas) off the screen, by introspecting canvas items: a card
per table, connectors ending at the right column rows, dragging a card and its connectors,
hover and selection, clicks, zoom and fit, the minimap, folding cards to their linked
columns, and many cards drawn lazily."""

import time
import types
import unittest

from tests.helpers import off_screen_windows, tk_root, within
from tests.test_erd import people_model, spec
from tests.test_linkgraph import hub_links, random_links
from engine import erd, limits
from engine.erd import ErdModel
from engine.linkgraph import DB
from erd_view import LAZY_CARDS, ErdCanvas
from tokens import COLOR


def ev(canvas, x, y, state=0, delta=0):
    """A fake mouse event at canvas coordinates (x, y)."""
    return types.SimpleNamespace(x=int(round(x - canvas.canvasx(0))),
                                 y=int(round(y - canvas.canvasy(0))), state=state, delta=delta)


class ViewCase(unittest.TestCase):
    def setUp(self):
        limits.reset()
        self.addCleanup(limits.reset)
        self.root = tk_root(self)
        off_screen_windows(self)
        self.calls = []
        self.view = ErdCanvas(self.root, on_table=lambda k: self.calls.append(("table", k)),
                              on_relationship=lambda r: self.calls.append(("rel", r)),
                              on_open=lambda n: self.calls.append(("open", n)),
                              on_positions=lambda p: self.calls.append(("pos", p)),
                              on_group=lambda g, e: self.calls.append(("group", g, e)))
        self.view.pack(fill="both", expand=True)
        self.root.geometry("1100x700")
        self.root.update_idletasks()

    def items(self, tag):
        return self.view.canvas.find_withtag(tag)

    def texts(self, tag):
        cv = self.view.canvas
        return sorted(cv.itemcget(i, "text") for i in cv.find_withtag(tag))


class ErdCanvasTest(ViewCase):
    def test_cards_rows_and_connector_ends(self):
        v = self.view
        m = people_model()
        v.show(m, focus=(DB, "message"))
        self.assertEqual(self.texts("name"), ["message", "message_tag", "person", "tag"])
        self.assertEqual(len(self.items("body")), 4)
        self.assertEqual(len(self.items("link")), len(m.rels))
        self.assertEqual(len(v.drawn_rels()), len(m.rels))
        self.assertIn("sender_id", self.texts("colname"))
        self.assertIn("INTEGER", self.texts("coltype"))
        self.assertEqual(len(self.items("fkmark")), 2 * 5)      # a badge: box and text
        self.assertEqual(len(self.items("pkmark")), 2 * 3)      # a key: ring and bit
        cv = v.canvas
        s = v.scale
        for r in m.rels:
            pts = cv.coords(v.rel_line(r.rid))
            x0, y0, x1, y1 = v.card_rect(r.src)
            self.assertAlmostEqual(pts[1], v.row_y(r.src, r.src_col), places=3)
            self.assertIn(round(pts[0], 2), (round(x0, 2), round(x1, 2)))
            self.assertAlmostEqual(pts[-1], v.row_y(r.dst, r.dst_col), places=3)
            # the row there shows the column's name
            hits = [i for i in cv.find_overlapping(x0 + 20 * s, pts[1] - 1, x1 - 5, pts[1] + 1)
                    if "colname" in cv.gettags(i)]
            self.assertEqual([cv.itemcget(i, "text") for i in hits], [r.src_col])
        # the focused card's header stands out; cardinality marks at both ends
        head = [i for i in cv.find_withtag(v._ctag[(DB, "message")]) if "head" in
                cv.gettags(i)][0]
        self.assertEqual(cv.itemcget(head, "fill"), COLOR["primary_soft"])
        self.assertEqual(len(self.items("mark")), 2 * len(m.rels))
        self.assertTrue(self.items("label"))
        # the colour of a connector says what it is
        declared = next(r for r in m.rels if r.src_col == "sender_id")
        verified = next(r for r in m.rels if r.src_col == "reply_to")
        self.assertEqual(cv.itemcget(v.rel_line(declared.rid), "dash"), "")
        self.assertNotEqual(cv.itemcget(v.rel_line(verified.rid), "dash"), "")

    def test_drag_moves_a_card_and_its_connectors(self):
        v = self.view
        m = people_model()
        v.show(m)
        cv = v.canvas
        key = (DB, "person")
        x0, y0, x1, y1 = v.card_rect(key)
        rel = next(r for r in m.rels if r.dst == key)
        before = cv.coords(v.rel_line(rel.rid))
        # press on the header, drag 60 px down and 40 px left, release
        v._on_press(ev(cv, x0 + 10, y0 + 5))
        v._on_drag(ev(cv, x0 + 5, y0 + 10))
        v._on_drag(ev(cv, x0 - 30, y0 + 65))
        v._on_release(ev(cv, x0 - 30, y0 + 65))
        nx0, ny0, _x1, _y1 = v.card_rect(key)
        self.assertAlmostEqual(nx0 - x0, -40, places=3)
        self.assertAlmostEqual(ny0 - y0, 60, places=3)
        after = cv.coords(v.rel_line(rel.rid))
        self.assertNotEqual(before, after)
        self.assertAlmostEqual(after[-1], v.row_y(key, rel.dst_col), places=3)
        self.assertIn(round(after[-2], 2), (round(nx0, 2), round(_x1, 2)))
        # every item of the card moved with it (its name too)
        name = [i for i in cv.find_withtag(v._ctag[key]) if "name" in cv.gettags(i)][0]
        self.assertAlmostEqual(cv.coords(name)[1], ny0 + erd.HEAD_H * v.scale / 2.0, places=3)
        # the positions were reported, and are what get_positions() says
        pos = [c for c in self.calls if c[0] == "pos"][-1][1]
        self.assertEqual(pos, v.get_positions())
        self.assertTrue(v.layout.manual)
        # a click (no movement) selects the card instead
        self.calls[:] = []
        v._on_press(ev(cv, nx0 + 10, ny0 + 5))
        v._on_release(ev(cv, nx0 + 10, ny0 + 5))
        self.assertEqual(self.calls, [("table", key)])
        self.assertEqual(v.selected, ("card", key))
        body = v._body[key]
        self.assertEqual(cv.itemcget(body, "outline"), COLOR["primary"])
        # tidy keeps it; auto-arrange forgets
        v.tidy()
        self.assertEqual(v.layout.overlaps(), [])
        v.auto_arrange()
        self.assertEqual(self.calls[-1], ("pos", None))
        self.assertFalse(v.layout.manual)

    def test_hover_highlights_a_relationship_end_to_end_and_click_fires(self):
        v = self.view
        m = people_model()
        v.show(m)
        cv = v.canvas
        rel = next(r for r in m.rels if r.src_col == "receiver_id")
        v.hover(("rel", rel.rid))
        line = v.rel_line(rel.rid)
        self.assertEqual(cv.itemcget(line, "fill"), COLOR["secondary"])
        self.assertEqual(float(cv.itemcget(line, "width")), 3.0)
        tints = self.items("hl")
        self.assertEqual(len(tints), 2)
        ys = sorted((cv.coords(t)[1] + cv.coords(t)[3]) / 2.0 for t in tints)
        self.assertEqual([round(y, 2) for y in ys], sorted(
            round(v.row_y(k, c), 2) for k, c in ((rel.src, rel.src_col),
                                                 (rel.dst, rel.dst_col))))
        for t in tints:
            self.assertEqual(cv.itemcget(t, "fill"), COLOR["primary_soft"])
        # the other connectors keep their style
        other = next(r for r in m.rels if r.src_col == "sender_id")
        self.assertEqual(float(cv.itemcget(v.rel_line(other.rid), "width")), 1.5)
        v.hover(None)
        self.assertEqual(self.items("hl"), ())
        self.assertEqual(float(cv.itemcget(line, "width")), 1.5)
        # the mouse over the line: the hit is the relationship; a click fires the callback
        pts = cv.coords(line)
        mx, my = (pts[0] + pts[2]) / 2.0, pts[1]
        self.assertEqual(v._hit(ev(cv, mx, my)), ("rel", rel.rid))
        v._on_motion(ev(cv, mx, my))
        self.assertEqual(len(self.items("hl")), 2)
        self.assertTrue(self.items("tip"))
        v._on_press(ev(cv, mx, my))
        v._on_release(ev(cv, mx, my))
        self.assertEqual(self.calls[-1], ("rel", rel))
        self.assertEqual(v.selected, ("rel", rel.rid))
        v._on_motion(ev(cv, -5000, -5000))
        self.assertEqual(len(self.items("hl")), 2)          # the selection stays tinted
        self.assertEqual(cv.itemcget(line, "fill"), COLOR["primary"])
        # Escape clears it
        v.clear_selection()
        self.assertEqual(self.items("hl"), ())
        self.assertEqual(self.calls[-1], ("table", None))
        # a double-click on a card opens the table
        x0, y0, x1, y1 = v.card_rect((DB, "tag"))
        v._on_double(ev(cv, x0 + 20, y0 + 5))
        self.assertEqual(self.calls[-1], ("open", (DB, "tag")))

    def test_zoom_fit_and_minimap(self):
        v = self.view
        v.show(people_model())
        s0 = v.scale
        v.zoom_center(1.25)
        self.assertAlmostEqual(v.scale, s0 * 1.25)
        v.zoom_center(0.2)
        self.assertTrue(v.text_hidden())
        self.assertFalse(v.rows_shown())
        self.assertEqual(self.items("row"), ())                 # headers only
        self.assertEqual(len(self.items("body")), 4)
        self.assertTrue(all(v.canvas.itemcget(i, "state") == "hidden"
                            for i in self.items("ztext")))
        v.fit()
        x0, y0, x1, y1 = v.layout.bounds
        vw, vh = v._view_size()
        self.assertAlmostEqual(v.scale, min(3.0, min(vw / (x1 - x0), vh / (y1 - y0))),
                               places=4)
        # the minimap: every card, and the viewport rectangle
        mm = v.minimap
        self.assertEqual(len(mm.find_withtag("mcard")), 4)
        vp = v.minimap_viewport()
        self.assertEqual(len(vp), 4)
        self.assertTrue(vp[2] > vp[0] and vp[3] > vp[1])
        # a click on the minimap moves the view there
        before = v.view_center()
        v._minimap_go(types.SimpleNamespace(x=2, y=2))
        self.assertNotEqual(v.view_center(), before)
        self.assertNotEqual(v.minimap_viewport(), vp)
        v.show_minimap(False)
        self.assertFalse(mm.winfo_manager())

    def test_linked_columns_only_and_per_card(self):
        v = self.view
        m = people_model()
        v.show(m)
        all_rows = len(self.items("colname"))
        self.assertEqual(all_rows, 2 + 5 + 2 + 2)
        v.set_linked_only(True)
        names = self.texts("colname")
        self.assertNotIn("body", names)
        self.assertNotIn("name", names)
        self.assertLess(len(names), all_rows)
        self.assertEqual(self.texts("more"), ["1 other column"] * 3)
        # connectors still end at their rows
        for r in m.rels:
            pts = v.canvas.coords(v.rel_line(r.rid))
            self.assertAlmostEqual(pts[-1], v.row_y(r.dst, r.dst_col), places=3)
        # one card listed again with its header's glyph
        v.set_linked_only(False)
        key = (DB, "message")
        glyph = [i for i in v.canvas.find_withtag(v._ctag[key]) if "toggle" in
                 v.canvas.gettags(i)][0]
        self.assertEqual(v.canvas.itemcget(glyph, "text"), "▾")
        v.click(("toggle", key))
        self.assertEqual(v.mode_of(key), "linked")
        self.assertEqual(len(self.items("colname")), all_rows - 1)       # body folded
        # 'N other columns' lists them again
        v.click(("more", key))
        self.assertEqual(len(self.items("colname")), all_rows)

    def test_groups_expand_and_mark_members(self):
        v = self.view
        m = ErdModel(hub_links(40))
        v.show(m)
        gid = next(iter(m.groups))
        self.assertEqual(self.texts("gname"), ["40 tables  ▸"])
        self.assertEqual(self.items("member"), ())
        v.click(("card", gid))
        self.assertEqual(self.calls[-2:], [("group", gid, True), ("table", gid)])
        self.assertEqual(len(self.items("member")), 40)
        self.assertEqual(v.layout.overlaps(), [])
        v.set_marks([(DB, "t003")])
        v.relayout()
        self.assertEqual(len(self.items("match")), 1)

    def test_many_cards_draw_rows_lazily(self):
        links = random_links(300, 520, seed=9)
        specs = {}
        for l in links:
            for n in (l.src, l.dst):
                specs.setdefault(n, spec("_id INTEGER pk", "a TEXT", "b TEXT",
                                         *["ref_%d INTEGER" % i for i in range(4)]))
        m = ErdModel(links, specs)
        self.assertGreater(len(m.tables), LAZY_CARDS)
        v = self.view
        t0 = time.perf_counter()
        v.show(m)
        v.set_scale(1.0)
        seconds = time.perf_counter() - t0
        self.assertEqual(len(self.items("name")), len(m.tables))      # every header
        drawn = len(v._rows_drawn)
        self.assertGreater(drawn, 0)
        self.assertLess(drawn, len(m.tables) // 2)                    # rows only near view
        within(self, seconds, 5.0, "diagram view")
        # moving the view draws the rows now in view
        far = max(m.tables, key=lambda k: v.layout.cards[k].x + v.layout.cards[k].y)
        v.see(far)
        self.assertIn(far, v._rows_drawn)
        # hovering stays cheap: only the connectors whose style changes are touched
        t0 = time.perf_counter()
        for r in m.rels[:50]:
            v.hover(("rel", r.rid))
        within(self, time.perf_counter() - t0, 2.0)


if __name__ == "__main__":
    unittest.main()
