"""Link graphs shaped like a messaging app's database (generated, no real data): a few hub
tables referred to by many others (in-degrees like 205, 82, 43, ...), most links found only
by names or partly matching values ('weaker'), about a third trusted."""

import random

from engine.erd import TableSpec
from engine.linkgraph import DB

HUB_IN = (205, 82, 43, 13, 12, 10, 10, 9, 7, 7, 7, 7)


def _mk(src, src_col, dst, dst_col, trusted):
    from tests.test_linkgraph import mk
    return mk(src, src_col, dst, dst_col, kind="verified" if trusted else "weaker")


def messaging_shape(tables=185, trusted=0.36, seed=5):
    """(links, specs): 12 hubs, `tables` other tables, ~430 links."""
    rnd = random.Random(seed)
    hubs = ["hub_%02d" % i for i in range(len(HUB_IN))]
    others = ["table_%03d" % i for i in range(tables)]
    links, seen, cols_of = [], set(), {}
    for h, n in zip(hubs, HUB_IN):
        k = 0
        while k < n:
            if rnd.random() < 0.85:
                src = others[min(int(rnd.expovariate(1.0) * 45), tables - 1)]
            else:
                src = rnd.choice(hubs)
            if src == h:
                continue
            col = "%s_row_id%s" % (h, "" if rnd.random() < 0.7 else "_%d" % rnd.randrange(3))
            if (src, col) in seen:
                continue
            seen.add((src, col))
            cols_of.setdefault(src, []).append(col)
            links.append(_mk(src, col, h, "_id", rnd.random() < trusted))
            k += 1
    for i in range(1, len(hubs)):                   # the hubs refer to each other
        links.append(_mk(hubs[i], "%s_row_id" % hubs[0], hubs[0], "_id", True))
    specs = {}
    for t in hubs + others:
        cols = [("_id", "INTEGER", 1)] + [(c, "INTEGER", 0) for c in cols_of.get(t, ())]
        cols += [("v%d" % j, "TEXT", 0) for j in range(rnd.randrange(1, 8))]
        specs[(DB, t)] = TableSpec(cols, {"_id": "PRIMARY KEY"}, rows=100)
    return links, specs
