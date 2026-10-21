"""Fields inside a column's values: the paths found in decoded BLOBs / JSON / plists, and a
path's value read out of one row's value - so a field inside a BLOB can be shown as a column
of its own.

A value is decoded with decode_blob() and rendered with Node.to_plain(); a path names one
value in that rendering, exactly as the data has it (no knowledge of any app is assumed):

    root.to[0].name          key 'root', then key 'to', its first item, then key 'name'
    ["odd key"].x            a key that is not a plain name is quoted

discover(values) lists the paths of a sample of values with how many values have each, the
kinds of value found and a few examples; read(value, paths) returns the first of the paths
that has a value in this one (several paths make one column: 'from.name, else
paidFrom[0].accountHolderName'), and which path it was. Neither ever raises.
"""

import json
import re

from . import CONFIDENT, decode_blob

_PLAIN_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$#]*\Z")
_STEP = re.compile(r'\.?([A-Za-z_$][A-Za-z0-9_$#]*)|\[(\d+)\]|\[("(?:[^"\\]|\\.)*")\]')
EXAMPLES = 3
MAX_LEAVES = 5000           # leaves read from one value (a huge plist is read in part)


def _key_step(key):
    return ("." + key) if _PLAIN_KEY.match(key) else "[%s]" % json.dumps(key)


def join(path, step):
    """path + one step: a key (str) or a list index (int)."""
    if isinstance(step, int):
        return "%s[%d]" % (path, step)
    s = _key_step(step)
    return s[1:] if not path and s.startswith(".") else path + s


def split(path):
    """The steps of a path: keys (str) and list indexes (int); None when malformed."""
    steps, pos = [], 0
    while pos < len(path):
        m = _STEP.match(path, pos)
        if not m or m.end() == pos:
            return None
        if m.group(1) is not None:
            steps.append(m.group(1))
        elif m.group(2) is not None:
            steps.append(int(m.group(2)))
        else:
            steps.append(json.loads(m.group(3)))
        pos = m.end()
    return steps


def plain(value):
    """The decoded rendering of one stored value (dict / list / scalar), or None when it has
    no structure to look into (NULL, a number, text that is not JSON or a plist...)."""
    if value is None or isinstance(value, (int, float)):
        return None
    if isinstance(value, str):
        s = value.lstrip()
        if not s[:1] in ("{", "[", "<"):
            return None             # plain text: nothing inside
    try:
        node = decode_blob(value)
        # only a confident reading has fields: an uncertain guess (or the raw bytes when
        # nothing decodes) must not make up paths
        if node.kind == "bytes" and (not node.children or
                                     node.children[0].confidence != CONFIDENT):
            return None
        out = node.to_plain()
    except Exception:               # noqa: BLE001 - an undecodable value has no fields
        return None
    return out if isinstance(out, (dict, list)) else None


def leaves(tree):
    """(path, value) of every scalar in a rendering, in order (at most MAX_LEAVES)."""
    out, stack = [], [("", tree)]
    while stack and len(out) < MAX_LEAVES:
        path, node = stack.pop()
        if isinstance(node, dict):
            for k in reversed(list(node)):
                stack.append((join(path, str(k)), node[k]))
        elif isinstance(node, list):
            for i in range(len(node) - 1, -1, -1):
                stack.append((join(path, i), node[i]))
        else:
            out.append((path, node))
    return out


def kind_of(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    if isinstance(v, str):
        return "text"
    return type(v).__name__


class Field(object):
    """One path found: in how many of the values read, the kinds of value, examples."""
    __slots__ = ("path", "count", "kinds", "examples", "order")

    def __init__(self, path, order):
        self.path, self.order = path, order
        self.count = 0
        self.kinds = {}
        self.examples = []

    def kinds_text(self):
        return ", ".join(k for k, _n in sorted(self.kinds.items(), key=lambda x: -x[1]))


class Discovery(object):
    """discover()'s answer: fields (in the order first met), values read, how many of them
    had fields inside, and whether the list was cut at max_paths."""

    def __init__(self):
        self.fields = []
        self.read = 0
        self.structured = 0
        self.cut = False
        self.formats = {}


def discover(values, max_paths=3000, cancel=None):
    """The paths found in the values (an iterable of stored values)."""
    d = Discovery()
    by_path = {}
    for v in values:
        if cancel is not None and cancel():
            break
        d.read += 1
        tree = plain(v)
        if tree is None:
            continue
        d.structured += 1
        seen = set()
        for path, leaf in leaves(tree):
            f = by_path.get(path)
            if f is None:
                if len(by_path) >= max_paths:
                    d.cut = True
                    continue
                f = by_path[path] = Field(path, len(by_path))
                d.fields.append(f)
            if path in seen:
                continue
            seen.add(path)
            f.count += 1
            k = kind_of(leaf)
            f.kinds[k] = f.kinds.get(k, 0) + 1
            if leaf not in (None, "") and len(f.examples) < EXAMPLES:
                text = leaf if isinstance(leaf, str) else json.dumps(leaf, default=str)
                if text not in f.examples:
                    f.examples.append(text[:120])
    return d


def get(tree, path):
    """The value at path in a rendering, or None when the path is not there."""
    steps = split(path)
    if steps is None:
        return None
    node = tree
    for s in steps:
        if isinstance(s, int):
            if not isinstance(node, list) or s >= len(node):
                return None
            node = node[s]
        else:
            if not isinstance(node, dict) or s not in node:
                return None
            node = node[s]
    if isinstance(node, (dict, list)):
        return json.dumps(node, ensure_ascii=False, default=str)
    return node


def read(value, paths, tree=None):
    """(value, path) of the first path that has a value (not None / '') in this stored
    value; (None, None) when none has. tree: the value's rendering when already decoded."""
    tree = plain(value) if tree is None else tree
    if tree is None:
        return None, None
    for p in paths:
        v = get(tree, p)
        if v not in (None, ""):
            return v, p
    return None, None
