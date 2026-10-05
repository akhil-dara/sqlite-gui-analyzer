"""The relationship diagram's layout (engine.linkgraph) on synthetic graphs: no two boxes
overlap, no line runs through a box, everything lies within the bounds, alike links are
grouped above the limit, the layout is deterministic and fast, and the SVG is well formed and
lists every grouped table."""

import random
import time
import unittest
import xml.dom.minidom

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from engine import limits
from engine.linkgraph import DB, Link, LinkGraph, overlaps, rects_overlap


def mk(src, src_col, dst, dst_col, kind="verified", direction="out", cross=False,
       overlap=0.98, db=DB):
    """A Link without a Relation behind it (the layout reads only these fields)."""
    l = Link.__new__(Link)
    l.relation = None
    l.src, l.src_col, l.dst, l.dst_col = (db, src), src_col, (db, dst), dst_col
    l.kind, l.direction, l.cross, l.confident = kind, direction, cross, kind != "weaker"
    l.score, l.reason, l.overlap, l.sampled = 0.9, "test", overlap, 200
    l.src_rows = l.dst_rows = 10
    return l


def hub_links(referring=150):
    """message referred to by `referring` tables alike (message_row_id -> _id), by 8 tables
    each in its own way, referring to chat and jid, a peer pair and a self-reference."""
    links = [mk("t%03d" % i, "message_row_id", "message", "_id") for i in range(referring)]
    links += [mk("u%02d" % i, "col_%d" % i, "message", "_id") for i in range(8)]
    links += [mk("message", "chat_row_id", "chat", "_id", kind="declared"),
              mk("message", "sender_jid_row_id", "jid", "_id"),
              mk("chat", "jid_row_id", "jid", "_id"),
              mk("u01", "jid_row_id", "jid", "_id"),
              mk("message", "key_id", "message_add_on", "key_id", direction="peer"),
              mk("message", "quoted_row_id", "message", "_id")]
    return links


def random_links(tables=275, count=500, seed=7):
    rnd = random.Random(seed)
    names = ["table_%03d" % i for i in range(tables)]
    out, seen = [], set()
    while len(out) < count:
        a = names[min(int(rnd.expovariate(1.0) * 30), tables - 1)]
        b = rnd.choice(names)
        col = "ref_%d" % rnd.randrange(4)
        if (a, b, col) in seen:
            continue
        seen.add((a, b, col))
        out.append(mk(b, col, a, "_id"))
    return out


class LayoutChecks(object):
    def check(self, lay):
        rects = lay.rects()
        self.assertEqual(overlaps(rects), [])
        x0, y0, x1, y1 = lay.bounds
        for key, x, y, w, h in rects:
            self.assertTrue(x0 <= x and y0 <= y and x + w <= x1 and y + h <= y1, key)
        for b in lay.boxes.values():
            inner = [(n, x, y, w, h) for n, x, y, w, h in b.member_rects]
            self.assertEqual(overlaps(inner), [])
            for n, x, y, w, h in inner:
                self.assertTrue(b.x <= x and x + w <= b.x + b.w and b.y + b.head_h <= y and
                                y + h <= b.y + b.h, n)
        # no line runs through a box: its segments only touch the boxes they start and end at
        for e in lay.edges:
            self.assertGreaterEqual(len(e.points), 2)
            for (ax, ay), (bx, by) in zip(e.points, e.points[1:]):
                self.assertTrue(abs(ax - bx) < 0.01 or abs(ay - by) < 0.01, "not at right angles")
                for key, x, y, w, h in rects:
                    self.assertFalse(_crosses((ax, ay, bx, by), (x, y, w, h)),
                                     "%r runs through %r" % (e.label, key))
            for x, y in e.points:
                self.assertTrue(x0 <= x <= x1 and y0 <= y <= y1)
        placed = [e.label_rect for e in lay.edges if e.label_rect]
        for i, r in enumerate(placed):
            for other in placed[i + 1:]:
                self.assertFalse(rects_overlap(r, other))
            for key, x, y, w, h in rects:
                self.assertFalse(rects_overlap(r, (x, y, w, h)), key)

    @staticmethod
    def signature(lay):
        return (sorted((repr(k), round(b.x, 2), round(b.y, 2), round(b.w, 2), round(b.h, 2))
                       for k, b in lay.boxes.items()),
                [[(round(x, 2), round(y, 2)) for x, y in e.points] for e in lay.edges])


def _crosses(seg, rect):
    """True when an axis-parallel segment enters the inside of a rectangle."""
    ax, ay, bx, by = seg
    x, y, w, h = rect
    e = 0.5
    lo_x, hi_x, lo_y, hi_y = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
    return lo_x < x + w - e and hi_x > x + e and lo_y < y + h - e and hi_y > y + e


class HubLayoutTest(unittest.TestCase, LayoutChecks):
    def setUp(self):
        limits.reset()
        self.addCleanup(limits.reset)

    def test_hub_with_150_referring_tables(self):
        g = LinkGraph(hub_links())
        t0 = time.time()
        lay = g.layout((DB, "message"))
        self.assertLess(time.time() - t0, 0.5)
        self.check(lay)
        groups = [b for b in lay.boxes.values() if b.kind == "group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].title, "150 tables")
        self.assertEqual(groups[0].lines[0][0], "message_row_id → message._id")
        self.assertEqual(len(groups[0].group.members), 150)
        self.assertEqual(len(groups[0].group.links[0]), 150)
        # the hub in the middle: referring tables left of it, referred tables right, the peer
        # below
        hub = lay.boxes[(DB, "message")]
        self.assertLess(groups[0].x + groups[0].w, hub.x)
        self.assertLess(lay.boxes[(DB, "u03")].x + lay.boxes[(DB, "u03")].w, hub.x)
        self.assertGreater(lay.boxes[(DB, "chat")].x, hub.x + hub.w)
        self.assertGreater(lay.boxes[(DB, "message_add_on")].y, hub.y + hub.h)
        # 12 tables in boxes + 1 group; one line per link, the group's 150 as one
        self.assertEqual(len(lay.boxes), 13)
        self.assertEqual(len(lay.edges), 8 + 1 + 3 + 1 + 1 + 1)
        # u01 -> jid and chat -> jid join two neighbours: drawn too
        self.assertEqual(lay.hidden, 0)
        # every link into message._id meets the same point of the hub (one trunk)
        ends = set(e.points[-1] for e in lay.edges if e.b == (DB, "message") and
                   e.pb == "_id" and e.a != (DB, "message"))
        self.assertEqual(len(ends), 1)
        self.assertEqual(self.signature(lay), self.signature(g.layout((DB, "message"))))

    def test_expanded_group_lists_its_tables_in_a_grid(self):
        g = LinkGraph(hub_links())
        gid = g.layout((DB, "message")).groups[0].gid
        lay = g.layout((DB, "message"), expanded=[gid])
        self.check(lay)
        box = lay.boxes[gid]
        self.assertTrue(box.expanded)
        self.assertEqual(len(box.member_rects), 150)
        self.assertEqual([n[1] for n, _x, _y, _w, _h in box.member_rects],
                         ["t%03d" % i for i in range(150)])
        self.assertGreater(len(set(round(x) for _n, x, _y, _w, _h in box.member_rects)), 2)
        self.assertIn((DB, "t042"), g.pos)

    def test_find_expands_the_group_to_its_matching_tables(self):
        g = LinkGraph(hub_links())
        lay = g.layout((DB, "message"), find="t04")
        self.check(lay)
        box = [b for b in lay.boxes.values() if b.kind == "group"][0]
        self.assertTrue(box.expanded)
        self.assertEqual([n[1] for n, *_r in box.member_rects], ["t%03d" % i
                                                                for i in range(40, 50)])
        self.assertIn("10 of 150 match", box.note)
        self.assertEqual(len(lay.matches), 10)
        lay = g.layout((DB, "message"), find="jid_row")
        self.assertEqual(set(n[1] for n in lay.matches), set(["chat", "u01", "message"]))

    def test_group_minimum_is_a_limit(self):
        g = LinkGraph(hub_links(referring=5))
        self.assertEqual(g.layout((DB, "message")).groups, [])
        limits.load({"limits": {"diagram_group_min": 3}})
        lay = g.layout((DB, "message"))
        self.assertEqual([b.title for b in lay.boxes.values() if b.kind == "group"],
                         ["5 tables"])
        self.check(lay)
        # without groups every table has its own box, still apart
        lay = LinkGraph(hub_links()).layout((DB, "message"), group_min=1000)
        self.assertEqual(len(lay.boxes), 162)
        self.check(lay)
        self.assertTrue(limits.validate({"diagram_group_min": 1})[1])

    def test_labels_only_when_few_lines(self):
        g = LinkGraph(hub_links(referring=0))
        lay = g.layout((DB, "message"))
        self.assertTrue(any(e.label_rect for e in lay.edges))
        self.check(lay)
        limits.load({"limits": {"diagram_edge_labels": 3}})
        lay = g.layout((DB, "message"))
        self.assertFalse(any(e.label_rect for e in lay.edges))

    def test_two_hops(self):
        links = hub_links(referring=10) + [mk("chat", "folder_id", "folder", "_id"),
                                           mk("t003", "extra_id", "extra", "_id")]
        g = LinkGraph(links)
        one = g.layout((DB, "message"))
        self.assertNotIn((DB, "folder"), one.boxes)
        two = g.layout((DB, "message"), hops=2)
        self.check(two)
        self.assertIn((DB, "folder"), two.boxes)
        self.assertGreater(two.boxes[(DB, "folder")].x, two.boxes[(DB, "chat")].x)
        # extra hangs off a grouped table: counted, not drawn
        self.assertNotIn((DB, "extra"), two.boxes)
        self.assertEqual(two.hidden_tables, 1)


class OverviewTest(unittest.TestCase, LayoutChecks):
    def test_275_tables_500_links(self):
        g = LinkGraph(random_links())
        t0 = time.time()
        lay = g.layout(None)
        took = time.time() - t0
        self.assertLess(took, 0.5)
        self.check(lay)
        self.assertEqual(self.signature(lay), self.signature(g.layout(None)))

    def test_components_and_leaf_groups(self):
        links = hub_links() + [mk("a", "b_id", "b", "_id"), mk("c", "b_id", "b", "_id")]
        g = LinkGraph(links)
        lay = g.layout(None)
        self.check(lay)
        self.assertEqual(lay.components, 2)
        self.assertEqual([b.title for b in lay.boxes.values() if b.kind == "group"],
                         ["150 tables"])
        # the most linked box is central in its block
        hub = lay.boxes[(DB, "message")]
        block = [b for k, b in lay.boxes.items() if k not in ((DB, "a"), (DB, "b"), (DB, "c"))]
        cx = (min(b.x for b in block) + max(b.x + b.w for b in block)) / 2.0
        self.assertLess(abs(hub.x + hub.w / 2.0 - cx), hub.w + 200)

    def test_svg_is_well_formed_and_lists_every_grouped_table(self):
        g = LinkGraph(hub_links() + [mk("x", "y_id", "y", "_id", cross=True, kind="value",
                                        db="contacts.db")])
        g.layout((DB, "message"))
        doc = xml.dom.minidom.parseString(g.svg(colors={"contacts.db": "#123456"}))
        texts = [t.firstChild.data for t in doc.getElementsByTagName("text") if t.firstChild]
        for i in range(150):
            self.assertIn("t%03d" % i, texts)
        self.assertIn("150 tables", texts)
        # the canvas layout is left as it was (the group collapsed)
        self.assertFalse(g.current.boxes[g.current.groups[0].gid].expanded)
        g.layout(None)
        svg = g.svg(colors={"contacts.db": "#123456"})
        xml.dom.minidom.parseString(svg)
        self.assertIn("#c25100", svg)
        self.assertIn('stroke="#123456"', svg)

    def test_empty_graph(self):
        g = LinkGraph([])
        lay = g.layout(None)
        self.assertEqual(lay.boxes, {})
        xml.dom.minidom.parseString(g.svg())


if __name__ == "__main__":
    unittest.main()
