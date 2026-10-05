"""Dispatcher: runs the decoders on a buffer, picks interpretations, recurses, bounds work.

Decoders run in three tiers:
  1. signature formats (plists, typedstream, compression, images, known file types);
  2. the text family (JSON, base64, UTF-8 / UTF-16 text);
  3. binary heuristics (16-byte UUID, 8-byte little-endian timestamp, protobuf, raw
     deflate), all "uncertain" except strongly structured protobuf.
When building a tree, a tier is only tried if the earlier ones produced nothing confident, so
e.g. a PNG is never also offered as "protobuf?". interpretations() runs every tier.

All work is bounded by a Context: nested-blob depth, total nodes, structural nesting (which
keeps Python recursion far from its limit) and total decompressed output. The limits are
counts, not clocks, so results are deterministic.
"""

from . import compress, detect, images, plists, protobuf, text, timestamps, typedstream
from .nodes import CONFIDENT, FAILED, UNCERTAIN, Attempt, Node

MiB = 1 << 20

# A confident result of the key kind hides these kinds from the tree (still listed by
# interpretations()).
SUPERSEDES = {
    "json": frozenset(("text",)),
    "base64": frozenset(("text",)),
}
MAX_UNCERTAIN = 3               # uncertain interpretations kept when nothing is confident
INT64_TIME_KINDS = frozenset(("filetime", "webkit_us", "unix_us", "unix_ns", "cocoa_ns",
                              "dotnet_ticks", "unix_ms"))
_RANK = {CONFIDENT: 0, UNCERTAIN: 1, FAILED: 2}


class Context(object):
    """Limits and counters for one top-level decode."""

    def __init__(self, max_depth=6, max_nodes=None, max_output=None,
                 max_total_output=None, max_nest=None, max_steps=None,
                 exhaustive=False, prefix="decode"):
        """Budgets left as None come from engine.limits: '<prefix>_max_nodes',
        '<prefix>_max_output', '<prefix>_total_output', 'decode_max_nest' and
        '<prefix>_max_steps' (prefix 'decode', or 'summary' for grid-cell summaries)."""
        from .. import limits
        lim = limits.current()
        self.prefix = prefix
        self.max_depth = max_depth          # nested blobs decoded below the root buffer
        self.max_nodes = lim[prefix + "_max_nodes"] if max_nodes is None else max_nodes
        self.max_output = (lim[prefix + "_max_output"] if max_output is None
                           else max_output)     # decompressed bytes per stream
        self.output_left = (lim[prefix + "_total_output"] if max_total_output is None
                            else max_total_output)
        self.max_nest = lim["decode_max_nest"] if max_nest is None else max_nest
        self.steps_left = lim[prefix + "_max_steps"] if max_steps is None else max_steps
        self.exhaustive = exhaustive        # run every tier (interpretations())
        self.lzma_memory = lim["decode_lzma_memory"]
        self.members = lim["decode_members"]
        self.lz_frames = lim["decode_lz_frames"]
        self.checksum_bytes = lim["decode_checksum_bytes"]
        self.protobuf_nest = lim["protobuf_max_nest"]
        self.protobuf_packed = lim["protobuf_max_packed"]
        self.typedstream_array = lim["typedstream_max_array"]
        self.nodes = 0
        self.nest = 0
        self.limits_hit = set()
        self.errors = []                    # decoder exceptions contained by run()

    _LIMIT_NAMES = {"depth": "%s_max_depth", "nodes": "%s_max_nodes",
                    "output": "%s_max_output / %s_total_output",
                    "nesting": "decode_max_nest", "work": "%s_max_steps",
                    "lzma memory": "decode_lzma_memory", "members": "decode_members",
                    "frames": "decode_lz_frames", "protobuf nesting": "protobuf_max_nest",
                    "packed values": "protobuf_max_packed",
                    "typedstream array": "typedstream_max_array"}

    def limits_text(self):
        """'nodes (limit decode_max_nodes), ...' for the limits this decode reached."""
        out = []
        for what in sorted(self.limits_hit):
            name = self._LIMIT_NAMES.get(what)
            if name:
                name = name.replace("%s", self.prefix)
                out.append("%s (limit %s)" % (what, name))
            else:
                out.append(what)
        return ", ".join(out)

    # -- counters -----------------------------------------------------------
    def node(self, kind, label=None, value=None, children=None, offset=None, length=None,
             confidence=CONFIDENT, note=""):
        self.nodes += 1
        return Node(kind, label, value, children, offset, length, confidence, note)

    def full(self):
        if self.nodes >= self.max_nodes:
            self.limits_hit.add("nodes")
            return True
        return False

    def all_views(self, depth):
        """True where every tier and alternative view should be produced: only the top
        buffer of an exhaustive decode (nested buffers still get normal trees)."""
        return self.exhaustive and depth == 0

    def step(self, n=1):
        """Charge n parse steps; False once the work budget is spent."""
        self.steps_left -= n
        if self.steps_left < 0:
            self.limits_hit.add("work")
            return False
        return True

    def can_nest(self):
        if self.nest >= self.max_nest:
            self.limits_hit.add("nesting")
            return False
        return True

    def output_cap(self):
        """Bytes one decompression may produce now."""
        return max(0, min(self.max_output, self.output_left))

    def used_output(self, n):
        self.output_left -= n

    # -- recursion helpers used by the decoders -----------------------------
    def bytes_node(self, data, depth, label=None, offset=None, length=None, note="",
                   skip=()):
        """A "bytes" node for data found at blob depth `depth`, with its interpretations as
        children when the limits allow."""
        data = bytes(data)
        node = self.node("bytes", label, data, None, offset,
                         len(data) if length is None else length, CONFIDENT, note)
        if not data:
            return node
        why = self._stop_reason(depth)
        if why:
            node.note = _join(node.note, "not decoded: " + why)
            return node
        self.nest += 1
        try:
            node.children = choose(run(data, self, depth, skip))
        finally:
            self.nest -= 1
        return node

    def string_node(self, value, depth, label=None, offset=None, length=None, note=""):
        """A "string" node; strings that hold JSON or meaningful base64 get that decode as a
        child."""
        node = self.node("string", label, value, None, offset, length, CONFIDENT, note)
        if len(value) >= 16 and depth < self.max_depth and self.nest < self.max_nest \
                and self.nodes < self.max_nodes:
            self.nest += 1
            try:
                inner = text.decode_inner_string(value, self, depth)
            finally:
                self.nest -= 1
            if inner is not None:
                node.children.append(inner)
        return node

    def _stop_reason(self, depth):
        if depth >= self.max_depth:
            self.limits_hit.add("depth")
            return "depth limit"
        if self.full():
            return "node limit"
        if not self.can_nest():
            return "nesting limit"
        return ""


def _join(a, b):
    return "%s; %s" % (a, b) if a else b


# -- tiers ------------------------------------------------------------------
def _signature_tier(data, ctx, depth, skip, kind, subtype):
    if kind is None or kind in skip:
        return []
    if kind == "bplist" or kind == "xml_plist":
        return plists.decode(data, ctx, depth, kind)
    if kind == "typedstream":
        return [typedstream.decode(data, ctx, depth)]
    if kind == "image":
        return [_image(data, ctx, subtype)]
    if kind == "file":
        node = ctx.node("file", value=subtype, offset=0, length=len(data))
        return [Attempt("file", CONFIDENT, node, "signature of " + subtype)]
    return [compress.decode(data, ctx, depth, kind)]


def _image(data, ctx, subtype):
    size = images.dimensions(data, subtype)
    node = ctx.node("image", value=subtype, offset=0, length=len(data))
    if size is None:
        node.confidence = UNCERTAIN
        node.note = "size not found in header"
        return Attempt("image", UNCERTAIN, node, subtype + " signature; header not parsed")
    node.note = "%d×%d" % size
    node.children = [ctx.node("int", "width", size[0]), ctx.node("int", "height", size[1])]
    return Attempt("image", CONFIDENT, node, "%s header, %d×%d" % ((subtype,) + size))


def _int64_time(data, ctx):
    """An 8-byte BLOB read as a little-endian 64-bit timestamp (e.g. a Windows FILETIME),
    only for fine-grained units, where arbitrary numbers rarely land in 1995-2035.
    Big-endian readings are not offered: on real data they were mostly accidents."""
    number = int.from_bytes(data, "little", signed=True)
    readings = [(kind, when) for kind, when in timestamps.guess(number, 1995, 2035)
                if kind in INT64_TIME_KINDS]
    if not readings:
        return None
    kind, when = readings[0]
    note = "little-endian int64 %d as %s" % (number, timestamps.LABELS[kind])
    if len(readings) > 1:
        note += "; also " + ", ".join("%s %s" % (timestamps.LABELS[k], w)
                                      for k, w in readings[1:3])
    node = ctx.node("date", value=when, offset=0, length=8, confidence=UNCERTAIN, note=note)
    return Attempt("date", UNCERTAIN, node, "8 bytes that read as a plausible timestamp")


def _heuristic_tier(data, ctx, depth, skip, signature_kind):
    # Fixed-size readings first: for 8/16-byte BLOBs they are likelier than protobuf.
    out = []
    if len(data) == 16:
        out.append(text.decode_uuid(data, ctx))
    if len(data) == 8:
        out.append(_int64_time(data, ctx))
    if "protobuf" not in skip:
        out.append(protobuf.decode(data, ctx, depth))
    if "deflate" not in skip and signature_kind is None:
        out.append(compress.decode_raw_deflate(data, ctx, depth))
    return [a for a in out if a is not None]


def run(data, ctx, depth, skip=()):
    """Every attempt for this buffer, tier by tier. Stops after the first tier with a
    confident result, except for the top buffer of an exhaustive (inspector) decode."""
    attempts = []
    kind, subtype = detect.magic(data)
    tiers = (("signature", lambda: _signature_tier(data, ctx, depth, skip, kind, subtype)),
             ("text", lambda: text.decode(data, ctx, depth, skip)),
             ("heuristic", lambda: _heuristic_tier(data, ctx, depth, skip, kind)))
    for name, tier in tiers:
        try:
            attempts.extend(tier())
        except Exception as e:  # noqa: BLE001 - one decoder's bug must not lose the others
            error = "decoder error: %s: %s" % (type(e).__name__, str(e)[:100])
            ctx.errors.append("%s tier: %s" % (name, error))
            attempts.append(Attempt(kind or name, FAILED, None, error))
        if not ctx.all_views(depth) and \
                any(a.primary and a.confidence == CONFIDENT for a in attempts):
            break
    return attempts


def choose(attempts):
    """The nodes a tree shows for a buffer: every confident primary interpretation, else up
    to MAX_UNCERTAIN uncertain ones; kinds superseded by a chosen kind are dropped."""
    usable = [a for a in attempts if a.primary and a.node is not None]
    picked = [a for a in usable if a.confidence == CONFIDENT]
    if not picked:
        picked = [a for a in usable if a.confidence == UNCERTAIN][:MAX_UNCERTAIN]
    hidden = set()
    for a in picked:
        hidden.update(SUPERSEDES.get(a.kind, ()))
    return [a.node for a in picked if a.kind not in hidden]


def rank(attempts):
    """Stable order: confident, then uncertain, then failed."""
    return sorted(attempts, key=lambda a: _RANK.get(a.confidence, 3))
