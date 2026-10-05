"""Hand-built sample BLOBs for the decode tests (standard library only)."""
import plistlib
import struct
import zlib

UID = plistlib.UID


# -- protobuf wire format ------------------------------------------------------------
def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def zigzag(n):
    return (n << 1) ^ (n >> 63)


def key(field, wire):
    return varint(field << 3 | wire)


def pb_varint(field, n):
    return key(field, 0) + varint(n & 0xFFFFFFFFFFFFFFFF)


def pb_bytes(field, payload):
    return key(field, 2) + varint(len(payload)) + payload


def pb_double(field, value):
    return key(field, 1) + struct.pack("<d", value)


def pb_float(field, value):
    return key(field, 5) + struct.pack("<f", value)


def pb_group(field, body):
    return key(field, 3) + body + key(field, 4)


NESTED = pb_bytes(1, b"nested text") + pb_varint(2, zigzag(-3))
PROTOBUF = (pb_bytes(1, b"hello world") + pb_varint(2, 150) + pb_bytes(3, NESTED)
            + pb_double(4, 3.5) + pb_bytes(5, varint(1) + varint(300) + varint(70000))
            + pb_float(6, 1.25) + pb_varint(7, -2))


# -- property lists -------------------------------------------------------------------
PLAIN_DICT = {"name": "Bob", "age": 42, "tags": ["a", "b"], "blob": b"\x00\x01"}
BPLIST = plistlib.dumps(PLAIN_DICT, fmt=plistlib.FMT_BINARY)
XML_PLIST = plistlib.dumps(PLAIN_DICT, fmt=plistlib.FMT_XML)
NSDATE_SECONDS = 600000000.0            # 2020-01-06T10:40:00Z


def keyed_archive(objects, top=None):
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver",
                           "$top": top or {"root": UID(1)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _cls(name, *supers):
    return {"$classname": name, "$classes": [name] + list(supers) + ["NSObject"]}


def sample_archive():
    """root: NSMutableDictionary {name: 'Alice', when: NSDate, blob: NSData(bplist),
    items: NSArray[NSDictionary{k: 42}, <cycle back to root>], thing: MyThing,
    link: NSURL, id: NSUUID, nothing: NSNull}."""
    objects = [
        "$null",
        {"$class": UID(2), "NS.keys": [UID(3), UID(4), UID(5), UID(6), UID(17), UID(20),
                                       UID(24), UID(27)],
         "NS.objects": [UID(7), UID(8), UID(10), UID(12), UID(18), UID(21), UID(25),
                        UID(28)]},
        _cls("NSMutableDictionary", "NSDictionary"),
        "name", "when", "blob", "items",
        "Alice",                                                        # 7
        {"$class": UID(9), "NS.time": NSDATE_SECONDS},                  # 8
        _cls("NSDate"),                                                 # 9
        {"$class": UID(11), "NS.data": BPLIST},                         # 10
        _cls("NSMutableData", "NSData"),                                # 11
        {"$class": UID(13), "NS.objects": [UID(14), UID(1)]},           # 12 (cycle)
        _cls("NSArray"),                                                # 13
        {"$class": UID(2), "NS.keys": [UID(15)], "NS.objects": [UID(16)]},  # 14
        "k", 42,                                                        # 15, 16
        "thing",                                                        # 17
        {"$class": UID(19), "title": UID(7), "count": 5},               # 18
        _cls("MyThing"),                                                # 19
        "link",                                                         # 20
        {"$class": UID(22), "NS.base": UID(0), "NS.relative": UID(23)},  # 21
        _cls("NSURL"),                                                  # 22
        "https://example.org/a?b=c",                                    # 23
        "id",                                                           # 24
        {"$class": UID(26), "NS.uuidbytes": bytes(range(16))},          # 25
        _cls("NSUUID"),                                                 # 26
        "nothing",                                                      # 27
        {"$class": UID(29)},                                            # 28
        _cls("NSNull"),                                                 # 29
    ]
    return keyed_archive(objects)


def cyclic_bplist():
    """bplist00 whose only object is an array containing itself (plistlib builds a
    self-containing list from it)."""
    body = b"bplist00" + b"\xa1\x00"        # object 0 at offset 8: array of 1 ref -> 0
    table = bytes([8])
    trailer = struct.pack(">6xBBQQQ", 1, 1, 1, 0, len(body))
    return body + table + trailer


# -- typedstream ----------------------------------------------------------------------
def ts_str(text):
    """New shared string: 0x84, length, bytes."""
    raw = text.encode("utf-8")
    return b"\x84" + bytes([len(raw)]) + raw


TS_TEXT = "Hi there :)"
# An NSMutableAttributedString as iOS/macOS stores message.attributedBody. Object numbers
# (#) count objects and classes in order of appearance; string numbers (s) count shared
# strings. A reference byte is 0x92 + index.
TYPEDSTREAM = b"".join([
    b"\x04\x0bstreamtyped",             # version 4, signature
    b"\x81\xe8\x03",                    # system version 1000 (0x81: int16 follows)
    ts_str("@"),                        # s0 type "@": one object follows
    b"\x84",                            # #0 new object
    b"\x84", ts_str("NSMutableAttributedString"), b"\x00",   # #1 class (s1), version 0
    b"\x84", ts_str("NSAttributedString"), b"\x00",          # #2 superclass (s2)
    b"\x84", ts_str("NSObject"), b"\x00",                    # #3 superclass (s3)
    b"\x85",                            # end of class chain (nil)
    b"\x92",                            # type: ref s0 "@"
    b"\x84",                            # #4 new object: the text
    b"\x84", ts_str("NSMutableString"), b"\x01",             # #5 class (s4), version 1
    b"\x84", ts_str("NSString"), b"\x01",                    # #6 superclass (s5)
    b"\x95",                            # superclass: ref #3 NSObject
    ts_str("+"),                        # s6 type "+": length-prefixed bytes
    bytes([len(TS_TEXT)]), TS_TEXT.encode(),
    b"\x86",                            # end of #4
    ts_str("iI"),                       # s7 type "iI": attribute run
    b"\x01", bytes([len(TS_TEXT)]),     # i=1, I=length
    b"\x92",                            # type "@"
    b"\x84",                            # #7 new object: attributes
    b"\x84", ts_str("NSDictionary"), b"\x00",                # #8 class (s8)
    b"\x95",                            # superclass: ref #3
    ts_str("i"), b"\x01",               # s9 type "i": 1 entry
    b"\x92", b"\x84", b"\x98",          # "@", #9 new object of class ref #6 NSString
    b"\x98", b"\x1d__kIMMessagePartAttributeName", b"\x86",  # type ref s6 "+", 29 bytes
    b"\x92", b"\x84",                   # "@", #10 new object
    b"\x84", ts_str("NSNumber"), b"\x00",                    # #11 class (s10)
    b"\x84", ts_str("NSValue"), b"\x00",                     # #12 superclass (s11)
    b"\x95",                            # superclass: ref #3
    ts_str("*"),                        # s12 type "*": C string
    b"\x84\x9b",                        # #13 new C string = ref s9 "i" (objCType)
    b"\x9b", b"\x00",                   # type ref s9 "i", value 0
    b"\x86",                            # end of #10 NSNumber
    b"\x86",                            # end of #7 NSDictionary
    b"\x86",                            # end of #0
])

# An NSArray holding the same NSString twice (the second is a reference to object #3).
TYPEDSTREAM_REFS = b"".join([
    b"\x04\x0bstreamtyped\x81\xe8\x03",
    ts_str("@"),                        # s0
    b"\x84",                            # #0 NSArray
    b"\x84", ts_str("NSArray"), b"\x00",  # #1 (s1)
    b"\x84", ts_str("NSObject"), b"\x00",  # #2 (s2)
    b"\x85",
    ts_str("i"), b"\x02",               # s3: count 2
    b"\x92", b"\x84",                   # "@", #3 new object
    b"\x84", ts_str("NSString"), b"\x01", b"\x94",           # #4 class (s4) : ref #2
    ts_str("+"), b"\x05hello", b"\x86",  # s5
    b"\x92", b"\x95",                   # "@", reference to object #3
    b"\x86",
])


# -- images ---------------------------------------------------------------------------
def png(width, height):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                                                 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def bmp(width, height):
    header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 24, 0, 0, 0, 0, 0, 0)
    return b"BM" + struct.pack("<IHHI", 14 + len(header), 0, 0, 54) + header


def webp_lossless(width, height):
    bits = (width - 1) | ((height - 1) << 14)
    body = b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", bits)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def tiff(width, height):
    entries = [(256, 3, 1, width), (257, 3, 1, height)]
    ifd = struct.pack("<H", len(entries)) + b"".join(
        struct.pack("<HHIHH", tag, typ, count, value, 0) for tag, typ, count, value in entries)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + b"\x00\x00\x00\x00"
