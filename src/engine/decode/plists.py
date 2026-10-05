"""Binary and XML property lists (via plistlib) and NSKeyedArchiver archives.

A plain plist becomes  bplist|xml_plist -> <top value tree>.
An NSKeyedArchiver archive becomes  bplist|xml_plist -> nskeyedarchive -> one child per $top
key, with every UID reference into $objects resolved into a clean tree: dictionaries, arrays,
strings, NSData (decoded again), NSDate (ISO 8601 UTC), NSURL, NSUUID, NSNull and custom
classes (kind "object", value = class name, children = the archived fields). Resolution is
cycle-safe: a reference back to an object that is still being resolved becomes a "uid" node.
"""

import datetime
import plistlib
import struct
import uuid

from .nodes import CONFIDENT, Attempt, failed
from .timestamps import iso, to_iso

try:
    from urllib.parse import urljoin
except ImportError:         # pragma: no cover - always present in CPython
    urljoin = None

_UID = getattr(plistlib, "UID", None)

SHARED_INLINE = 32          # nodes up to which a shared archived object is repeated inline
_DICT_KEYS = frozenset(("NS.keys", "NS.objects"))
_STRING_KEYS = (frozenset(("NS.string",)), frozenset(("NS.bytes",)))
_URL_KEYS = frozenset(("NS.base", "NS.relative"))


def decode(data, ctx, depth, kind):
    """Attempts for a buffer that starts like a binary ("bplist") or XML plist."""
    if kind == "bplist":
        version = data[6:8]
        if version != b"00":
            return [failed("bplist", "binary plist version %r is not supported"
                           % version.decode("latin-1"))]
        problem = _trailer_problem(data)
        if problem:
            return [failed("bplist", problem)]
        fmt = plistlib.FMT_BINARY
    else:
        fmt = plistlib.FMT_XML
    try:
        value = plistlib.loads(bytes(data), fmt=fmt)
    except Exception as e:      # noqa: BLE001 - InvalidFileException, ExpatError, Overflow...
        return [failed(kind, "plistlib cannot read it: %s" % (str(e)[:100] or type(e).__name__))]
    return from_value(value, data, ctx, depth, kind)


def _trailer_problem(data):
    """Validate the 32-byte bplist00 trailer before plistlib sizes anything from it."""
    if len(data) < 8 + 1 + 32:
        return "too short for a binary plist"
    offset_size, ref_size, count, top, table = struct.unpack(">6xBBQQQ", data[-32:])
    if not 1 <= offset_size <= 8 or not 1 <= ref_size <= 8:
        return "bad offset/reference sizes in the trailer"
    if count == 0 or top >= count:
        return "bad object count or top object in the trailer"
    if table < 8 or table + count * offset_size > len(data) - 32:
        return "offset table lies outside the data (truncated plist?)"
    return ""


def from_value(value, data, ctx, depth, kind):
    """Attempts for an already parsed plist value."""
    archive = _keyed_archive(value)
    root = ctx.node(kind, offset=0, length=len(data))
    if archive is None:
        root.children.append(_Plain(ctx, depth).node(value, None))
        root.note = _shape(value)
        return [Attempt(kind, CONFIDENT, root, "%s property list, top %s"
                        % ("binary" if kind == "bplist" else "XML", _shape(value)))]
    archiver, top, objects = archive
    resolved = _Resolver(objects, ctx, depth).archive(archiver, top)
    root.children.append(resolved)
    root.note = "NSKeyedArchiver archive"
    attempts = [Attempt(kind, CONFIDENT, root, "%s archive, %d objects, resolved from $top"
                        % (archiver, len(objects)))]
    if ctx.all_views(depth):
        raw = ctx.node(kind, offset=0, length=len(data), note="raw archive (UIDs unresolved)")
        raw.children.append(_Plain(ctx, depth).node(value, None))
        attempts.append(Attempt(kind, CONFIDENT, raw, "raw property list without resolving "
                                "the archive's UID references", primary=False))
    return attempts


def _shape(value):
    if isinstance(value, dict):
        return "dict (%d keys)" % len(value)
    if isinstance(value, list):
        return "array (%d items)" % len(value)
    return type(value).__name__


def _uid_value(value):
    """The integer of a plistlib.UID (or an XML {"CF$UID": n} dict), else None."""
    if _UID is not None and isinstance(value, _UID):
        return value.data
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get("CF$UID"), int):
        return value["CF$UID"]
    return None


def _keyed_archive(value):
    """(archiver, $top, $objects) when value looks like a keyed archive, else None."""
    if not isinstance(value, dict):
        return None
    top, objects = value.get("$top"), value.get("$objects")
    archiver = value.get("$archiver")
    if not isinstance(top, dict) or not isinstance(objects, list):
        return None
    if not isinstance(archiver, str):
        archiver = "keyed archive"
    return archiver, top, objects


def _date_node(ctx, label, dt, note=""):
    return ctx.node("date", label, iso(dt), note=note)


class _Plain(object):
    """Plain plist values -> nodes. Cycle-safe: plistlib turns reference cycles in a binary
    plist into self-containing lists/dicts."""

    def __init__(self, ctx, depth):
        self.ctx, self.depth = ctx, depth
        self.active = set()

    def node(self, value, label):
        ctx = self.ctx
        uid = _uid_value(value)
        if uid is not None:
            return ctx.node("uid", label, uid)
        if isinstance(value, (dict, list)):
            return self._container(value, label)
        return _scalar(ctx, self.depth, value, label)

    def _container(self, value, label):
        ctx = self.ctx
        is_dict = isinstance(value, dict)
        node = ctx.node("dict" if is_dict else "array", label)
        if id(value) in self.active:
            node.note = "cycle: contains itself"
            return node
        if not ctx.can_nest():
            node.note = "nesting limit reached"
            return node
        self.active.add(id(value))
        ctx.nest += 1
        try:
            items = value.items() if is_dict else enumerate(value)
            for n, (key, item) in enumerate(items):
                if ctx.full():
                    node.note = "node limit reached after %d entries" % n
                    break
                node.children.append(self.node(item, key if is_dict else n))
        finally:
            ctx.nest -= 1
            self.active.discard(id(value))
        return node


def _scalar(ctx, depth, value, label, note=""):
    if isinstance(value, str):
        return ctx.string_node(value, depth, label, note=note)
    if isinstance(value, bool):
        return ctx.node("bool", label, value, note=note)
    if isinstance(value, int):
        return ctx.node("int", label, value, note=note)
    if isinstance(value, float):
        return ctx.node("float", label, value, note=note)
    if isinstance(value, (bytes, bytearray)):
        return ctx.bytes_node(value, depth + 1, label, note=note)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return _date_node(ctx, label, value, note)
    if value is None:
        return ctx.node("null", label, note=note)
    return ctx.node("string", label, repr(value), note="unexpected plist value type")


class _Resolver(object):
    """Resolves an NSKeyedArchiver $objects table into a node tree."""

    def __init__(self, objects, ctx, depth):
        self.objects, self.ctx, self.depth = objects, ctx, depth
        self.active = set()     # ("uid", n) / ("id", id(obj)) being resolved: cycle detection
        self.expanded = {}      # uid -> nodes its first expansion took

    def archive(self, archiver, top):
        ctx = self.ctx
        node = ctx.node("nskeyedarchive", value=archiver,
                        note="%d objects" % len(self.objects))
        for key, value in top.items():
            if ctx.full():
                node.note += "; node limit reached"
                break
            node.children.append(self.value(value, key))
        return node

    # -- any archived value -----------------------------------------------------
    def value(self, value, label):
        uid = _uid_value(value)
        if uid is not None:
            return self.ref(uid, label)
        if isinstance(value, (dict, list)):
            return self._guarded(("id", id(value)), value, label)
        return _scalar(self.ctx, self.depth, value, label)

    def ref(self, uid, label):
        ctx = self.ctx
        if not 0 <= uid < len(self.objects):
            return ctx.node("uid", label, uid, note="reference outside $objects")
        if ("uid", uid) in self.active:
            return ctx.node("uid", label, uid,
                            note="cycle: refers back to object %d (%s)"
                            % (uid, self._class_name(self.objects[uid]) or "value"))
        obj = self.objects[uid]
        if obj == "$null":
            return ctx.node("null", label)
        if _uid_value(obj) is not None:
            return ctx.node("uid", label, uid, note="object %d is itself a bare reference" % uid)
        if isinstance(obj, (dict, list)):
            return self._guarded(("uid", uid), obj, label)
        return _scalar(ctx, self.depth, obj, label)

    def _guarded(self, key, obj, label):
        """Resolve a container/instance with cycle, nesting and node-budget guards.

        An object referenced from several places is expanded each time while small; once
        its expansion took more than SHARED_INLINE nodes, later references become "uid"
        nodes pointing back to it (keeps shared object graphs linear in size)."""
        ctx = self.ctx
        uid = key[1] if key[0] == "uid" else None
        if key in self.active:
            return ctx.node("array" if isinstance(obj, list) else "dict", label,
                            note="cycle: contains itself")
        if self.expanded.get(uid, 0) > SHARED_INLINE:
            return ctx.node("uid", label, uid, note="object %d again (%s), expanded earlier"
                            % (uid, self._class_name(obj) or "container"))
        if not ctx.can_nest() or ctx.full():
            return ctx.node("uid", label, uid, note="not expanded: decode limit reached")
        self.active.add(key)
        ctx.nest += 1
        before = ctx.nodes
        try:
            if isinstance(obj, dict) and "$class" in obj:
                return self._instance(obj, label)
            return self._container(obj, label)
        finally:
            ctx.nest -= 1
            self.active.discard(key)
            if uid is not None and uid not in self.expanded:
                self.expanded[uid] = ctx.nodes - before

    def _container(self, obj, label):
        ctx = self.ctx
        is_dict = isinstance(obj, dict)
        node = ctx.node("dict" if is_dict else "array", label)
        items = obj.items() if is_dict else enumerate(obj)
        for n, (key, item) in enumerate(items):
            if ctx.full():
                node.note = "node limit reached after %d entries" % n
                break
            node.children.append(self.value(item, key))
        return node

    # -- class instances --------------------------------------------------------
    def _class_info(self, obj):
        """(class name, [class chain]) from an instance's $class reference."""
        uid = _uid_value(obj.get("$class"))
        info = self.objects[uid] if uid is not None and 0 <= uid < len(self.objects) else None
        if not isinstance(info, dict):
            return "?", []
        name = info.get("$classname")
        chain = [c for c in info.get("$classes") or [] if isinstance(c, str)]
        return (name if isinstance(name, str) else "?"), chain

    def _class_name(self, obj):
        if isinstance(obj, dict) and "$class" in obj:
            return self._class_info(obj)[0]
        return ""

    def _instance(self, obj, label):
        ctx = self.ctx
        name, chain = self._class_info(obj)
        fields = frozenset(k for k in obj if k != "$class")
        # Well-known Foundation shapes are recognised by their exact field set, so a custom
        # class that merely has one of these keys keeps all of its fields.
        if fields == _DICT_KEYS:
            return self._dictionary(obj, label, name)
        if fields == frozenset(("NS.objects",)):
            return self._sequence(obj.get("NS.objects"), label, name)
        if fields in _STRING_KEYS:
            text = self._string_payload(obj)
            if text is not None:
                return ctx.string_node(text, self.depth, label, note=name)
        if fields == frozenset(("NS.data",)):
            raw = self._deref(obj["NS.data"])
            if isinstance(raw, (bytes, bytearray)):
                return ctx.bytes_node(raw, self.depth + 1, label, note=name)
        if fields == frozenset(("NS.time",)):
            seconds = self._deref(obj["NS.time"])
            text = to_iso(seconds, "cocoa_s")
            if text is not None:
                return ctx.node("date", label, text,
                                note="%s: %r s since 2001-01-01" % (name, seconds))
        if fields and fields <= _URL_KEYS:
            url = self._url(obj)
            if url is not None:
                return ctx.string_node(url, self.depth, label, note=name)
        if fields == frozenset(("NS.uuidbytes",)):
            raw = self._deref(obj["NS.uuidbytes"])
            if isinstance(raw, (bytes, bytearray)) and len(raw) == 16:
                return ctx.node("string", label, str(uuid.UUID(bytes=bytes(raw))), note=name)
        if name == "NSNull" and not fields:
            return ctx.node("null", label, note=name)
        node = ctx.node("object", label, name)
        if len(chain) > 1:
            node.note = " : ".join(chain)
        for n, key in enumerate(k for k in obj if k != "$class"):
            if ctx.full():
                node.note = (node.note + "; " if node.note else "") + "node limit reached"
                break
            node.children.append(self.value(obj[key], key))
        return node

    def _deref(self, value):
        """A field value with one level of UID indirection removed (NSData bytes, NS.time)."""
        uid = _uid_value(value)
        if uid is not None and 0 <= uid < len(self.objects):
            return self.objects[uid]
        return value

    def _string_payload(self, obj):
        if "NS.string" in obj:
            value = self._deref(obj["NS.string"])
            return value if isinstance(value, str) else None
        value = self._deref(obj.get("NS.bytes"))
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", "replace")
        return None

    def _url(self, obj, hops=0):
        """NSURL text: NS.relative resolved against NS.base (itself possibly an NSURL)."""
        rel = self._deref(obj.get("NS.relative"))
        base = self._deref(obj.get("NS.base"))
        if isinstance(base, dict) and "$class" in base:
            base = self._url(base, hops + 1) if hops < 8 else None
        if base == "$null":
            base = None
        if not isinstance(rel, str):
            return None
        if isinstance(base, str) and urljoin is not None:
            return urljoin(base, rel)
        return rel

    def _dictionary(self, obj, label, name):
        ctx = self.ctx
        node = ctx.node("dict", label, name)
        keys = self._deref_list(obj.get("NS.keys"))
        values = self._deref_list(obj.get("NS.objects"))
        if len(keys) != len(values):
            node.note = "%d keys but %d values" % (len(keys), len(values))
        for n, (key, value) in enumerate(zip(keys, values)):
            if ctx.full():
                node.note = "node limit reached after %d entries" % n
                break
            key_node = self.value(key, None)
            if key_node.kind == "string":
                key_label = key_node.value
            else:
                key_label = key_node.value if key_node.value is not None else "#%d" % n
            node.children.append(self.value(value, key_label))
        return node

    def _deref_list(self, value):
        value = self._deref(value)
        return value if isinstance(value, list) else []

    def _sequence(self, items, label, name):
        ctx = self.ctx
        node = ctx.node("array", label, name)
        for n, item in enumerate(self._deref_list(items)):
            if ctx.full():
                node.note = "node limit reached after %d entries" % n
                break
            node.children.append(self.value(item, n))
        return node
