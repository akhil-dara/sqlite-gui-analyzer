"""Byte-level matching inside stored values: hex patterns, and text in UTF-8 / UTF-16LE / UTF-16BE.

A BLOB holds bytes, not text. A text term is looked for as the bytes each encoding gives it, at
any byte offset (UTF-16 text embedded at an odd offset is found too). Hex patterns match whole
bytes only: "48 65" never matches the nibbles "4865" of b"\\x14\\x86\\x5c".

Every match is a span (start, end, label) of byte offsets into the value; label names the
encoding the match was read in: "utf-8", "utf-16le", "utf-16be" or "hex".
"""

import re

# (label, Python codec) in the order matches are preferred when they start at the same offset
TEXT_ENCODINGS = (("utf-8", "utf-8"), ("utf-16le", "utf-16-le"), ("utf-16be", "utf-16-be"))
BOMS = {"utf-8": b"\xef\xbb\xbf", "utf-16le": b"\xff\xfe", "utf-16be": b"\xfe\xff"}
TERMINATORS = {"utf-8": b"\x00", "utf-16le": b"\x00\x00", "utf-16be": b"\x00\x00"}
# The ways a regex sees a BLOB as text: (label, codec, first byte)
REGEX_VIEWS = (("utf-8", "utf-8", 0), ("utf-16le", "utf-16-le", 0), ("utf-16le", "utf-16-le", 1),
               ("utf-16be", "utf-16-be", 0), ("utf-16be", "utf-16-be", 1))
SNIPPET_CONTEXT = 24        # bytes shown on each side of a match
SNIPPET_HEX_MAX = 32        # matched bytes shown as hex
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_HEX_SEPARATORS = re.compile(r"[\s,:]+")


# -- hex patterns --------------------------------------------------------------
def _hex_byte(pair, group):
    if pair == "??":
        return None
    if pair[0] in _HEX_DIGITS and pair[1] in _HEX_DIGITS:
        return int(pair, 16)
    if "?" in pair:
        raise ValueError("'%s' in '%s': a wildcard is '??' (one whole byte); half-byte wildcards "
                         "are not supported" % (pair, group))
    bad = pair[0] if pair[0] not in _HEX_DIGITS else pair[1]
    raise ValueError("'%s' in '%s' is not a hex digit" % (bad, group))


def parse_hex(text):
    """Parse a hex byte pattern into a list of byte values, None standing for a '??' wildcard.

    Accepted: digits in either case ("48656c", "48 65 6C"), separated by spaces, commas or colons
    ("48,65", "48:65"), with "0x" prefixes ("0x48 0x65", "0x48656c") or "\\x" escapes
    ("\\x48\\x65"), and "??" for any one byte ("48 ?? 6c"). Every byte needs both digits.
    Raises ValueError with a readable message for anything else.
    """
    tokens = []
    for group in _HEX_SEPARATORS.split(text.strip()):
        pos = 2 if group[:2] in ("0x", "0X") else 0
        while pos < len(group):
            if group[pos] == "\\":
                pair = group[pos + 2:pos + 4]
                if group[pos + 1:pos + 2] not in ("x", "X") or len(pair) < 2:
                    raise ValueError("'%s': a '\\' must start an escape like \\x4f" % group)
                tokens.append(_hex_byte(pair, group))
                pos += 4
                continue
            end = group.find("\\", pos)
            end = len(group) if end < 0 else end
            run = group[pos:end]
            if len(run) % 2:
                for c in run:
                    if c not in _HEX_DIGITS and c != "?":
                        raise ValueError("'%s' in '%s' is not a hex digit" % (c, group))
                if "?" in run:
                    raise ValueError("'%s': a wildcard is '??' (one whole byte); half-byte "
                                     "wildcards are not supported" % group)
                raise ValueError("'%s' has an odd number of hex digits: every byte needs two"
                                 % group)
            for i in range(0, len(run), 2):
                tokens.append(_hex_byte(run[i:i + 2], group))
            pos = end
    if not tokens:
        raise ValueError("enter hex bytes, e.g. 4A 6F ?? 6E")
    if all(t is None for t in tokens):
        raise ValueError("a hex pattern needs at least one known byte besides ?? wildcards")
    return tokens


class HexPattern(object):
    """A parsed hex pattern, matched at byte boundaries only."""

    def __init__(self, text):
        self.tokens = parse_hex(text)
        self.length = len(self.tokens)
        runs, run = [], bytearray()
        for t in self.tokens + [None]:
            if t is None:
                if run:
                    runs.append(bytes(run))
                run = bytearray()
            else:
                run.append(t)
        # Every match contains this run of known bytes (the SQL pre-filter looks for it)
        self.literal = max(runs, key=len)
        self.regex = None
        if len(self.literal) < self.length:
            self.regex = re.compile(b"".join(b"." if t is None else re.escape(bytes([t]))
                                             for t in self.tokens), re.DOTALL)

    def find(self, data):
        """Byte offset of the first match in data, or None."""
        if self.regex is None:
            i = data.find(self.literal)
            return i if i >= 0 else None
        if self.literal not in data:
            return None
        m = self.regex.search(data)
        return m.start() if m else None


# -- text inside bytes -----------------------------------------------------------
def case_forms(ch):
    """The characters a case-insensitive search treats as ch: those with the same lower case
    (single characters only: a sharp s does not match 'SS', as with str.lower())."""
    low = ch.lower()
    forms = set([ch])
    for f in (low, ch.upper(), low.upper(), ch.upper().lower()):
        if len(f) == 1 and f.lower() == low:
            forms.add(f)
    return forms


def _encode(s, codec):
    return s.encode(codec, "surrogatepass")


def _char_pattern(forms, codec):
    alts = sorted(set(_encode(f, codec) for f in forms))
    if len(alts) == 1:
        return re.escape(alts[0]), len(alts[0])
    longest = max(len(a) for a in alts)
    if longest == 1:
        return b"[" + b"".join(re.escape(a) for a in alts) + b"]", 1
    return b"(?:" + b"|".join(re.escape(a) for a in alts) + b")", longest


def _isascii(s):
    return all(ord(c) < 128 for c in s)


def invariant_run(term, strict=False):
    """Longest run of characters that match only themselves when case is ignored ("" if none).

    strict=True keeps only characters a regular expression with IGNORECASE cannot match to any
    other character either (no letters): used for regex literals.
    """
    best, run = "", []
    for c in term:
        if strict:
            ok = not c.isalpha() and c.lower() == c == c.upper()
        else:
            ok = len(case_forms(c)) == 1
        if ok:
            run.append(c)
            continue
        if len(run) > len(best):
            best = "".join(run)
        run = []
    if len(run) > len(best):
        best = "".join(run)
    return best


# ASCII letters a Unicode IGNORECASE regex also matches to non-ASCII characters: i to U+0130 and
# U+0131, k to U+212A, s to U+017F (the only ones, in Python 3.8-3.14)
_WIDE_CASE = frozenset("iksIKS")


def ascii_case_run(text):
    """Longest run of ASCII characters that match (even in a regex ignoring case) only
    themselves or their other ASCII case: every ASCII character but i, k and s."""
    best, run = "", []
    for c in text + "\x80":
        if ord(c) < 128 and c not in _WIDE_CASE:
            run.append(c)
            continue
        if len(run) > len(best):
            best = "".join(run)
        run = []
    return best


def needles(text):
    """text as bytes in each of the three encodings."""
    return [_encode(text, codec) for _label, codec in TEXT_ENCODINGS]


_ORDER = dict((label, i) for i, (label, _codec) in enumerate(TEXT_ENCODINGS))


def _ascii_units_around(data, start, end, label, most=64):
    """Printable ASCII UTF-16 code units (in label's byte order) right before and after a match."""
    big = label == "utf-16be"

    def text_unit(k):
        u = (data[k] << 8 | data[k + 1]) if big else (data[k] | data[k + 1] << 8)
        return 0x20 <= u < 0x7f
    n, k = 0, start - 2
    while k >= 0 and n < most and text_unit(k):
        n, k = n + 1, k - 2
    k = end
    while k + 1 < len(data) and n < 2 * most and text_unit(k):
        n, k = n + 1, k + 2
    return n


def pick_match(data, found):
    """The match to report among (start, end, label) candidates: the earliest, UTF-8 first at
    the same offset. Text of ASCII characters in UTF-16 also reads in the other byte order one
    byte earlier or later ('20 00 68 00 65 00' holds LE 'he' at 2 and BE 'he' at 1): then the
    reading surrounded by more ASCII text wins, else the one at an even offset."""
    if not found:
        return None
    found.sort(key=lambda c: (c[0], _ORDER[c[2]]))
    best = found[0]
    if best[2] == "utf-8":
        return best
    for other in found[1:]:
        if other[0] == best[0] + 1 and other[2] not in ("utf-8", best[2]):
            a = _ascii_units_around(data, best[0], best[1], best[2])
            b = _ascii_units_around(data, other[0], other[1], other[2])
            if b > a or (b == a and best[0] % 2):
                return other
            break
    return best


class TextInBytes(object):
    """A text term matched inside bytes as UTF-8, UTF-16LE and UTF-16BE.

    fold_case: letters match their other case (ASCII, and letters whose upper/lower case is one
    character). anchor: None (anywhere), "start", "end" or "whole" (the value is the term); an
    anchored match may have a byte order mark before it and one NUL terminator after it.
    """

    def __init__(self, term, fold_case=False, anchor=None):
        self.term, self.fold, self.anchor = term, fold_case, anchor
        self.ascii = _isascii(term)
        self.lowered = fold_case and anchor is None and self.ascii
        self.variants = []          # (label, needle, compiled regex, longest match in bytes)
        forms = [case_forms(c) for c in term] if fold_case else None
        for label, codec in TEXT_ENCODINGS:
            if self.lowered:
                self.variants.append((label, _encode(term.lower(), codec), None, 0))
                continue
            if not fold_case and anchor is None:
                self.variants.append((label, _encode(term, codec), None, 0))
                continue
            if fold_case:
                parts = [_char_pattern(f, codec) for f in forms]
                core = b"".join(p for p, _n in parts)
                span = sum(n for _p, n in parts)
            else:
                core = re.escape(_encode(term, codec))
                span = len(_encode(term, codec))
            bom = b"(?:" + re.escape(BOMS[label]) + b")?"
            nul = b"(?:" + re.escape(TERMINATORS[label]) + b")?\\Z"
            if anchor == "start":
                pattern = bom + b"(" + core + b")"
            elif anchor == "end":
                pattern = b"(" + core + b")" + nul
            elif anchor == "whole":
                pattern = bom + b"(" + core + b")" + nul
            else:
                pattern = b"(" + core + b")"
            self.variants.append((label, None, re.compile(pattern, re.DOTALL),
                                  span + len(TERMINATORS[label])))

    def sql_needles(self):
        """Bytes one of which every match contains, or [] when no such bytes are known."""
        if not self.fold:
            return needles(self.term)
        run = invariant_run(self.term)
        return needles(run) if run else []

    def search(self, data):
        """(start, end, label) of the first match in data (see pick_match), or None."""
        found = []
        low = data.lower() if self.lowered else None      # bytes.lower() only folds ASCII
        for label, needle, regex, span in self.variants:
            if needle is not None:
                i = (low if low is not None else data).find(needle)
                if i >= 0:
                    found.append((i, i + len(needle), label))
                continue
            if self.anchor in ("start", "whole"):
                m = regex.match(data)
            elif self.anchor == "end":
                m = regex.search(data, max(0, len(data) - span))
            else:
                m = regex.search(data)
            if m is not None:
                found.append((m.start(1), m.end(1), label))
        return pick_match(data, found)


def regex_in_bytes(rx, data):
    """(start, end, label) of the first match (see pick_match) of the compiled text regex rx in
    data read as UTF-8 (each invalid byte becomes one placeholder character that letters and
    digits never match) and as UTF-16LE / UTF-16BE from byte 0 and from byte 1; None if none."""
    found = []
    for label, codec, first in REGEX_VIEWS:
        if codec == "utf-8":
            errors, seg = "surrogateescape", data
        else:
            errors = "surrogatepass"
            seg = data[first:first + (len(data) - first) // 2 * 2]
        try:
            text = seg.decode(codec, errors)
        except UnicodeDecodeError:
            continue
        m = rx.search(text)
        if m is None:
            continue
        start = first + len(text[:m.start()].encode(codec, errors))
        found.append((start, start + len(m.group(0).encode(codec, errors)), label))
    return pick_match(data, found)


# -- display -------------------------------------------------------------------------
_REPLACEMENT = chr(0xfffd)      # what a decoder puts for bytes it cannot read


def _printable(s):
    return "".join(c if c.isprintable() and c != _REPLACEMENT else "." for c in s)


def render_bytes(seg, label):
    """Bytes as readable text: decoded in the match's encoding, or ASCII for hex matches;
    anything unprintable is shown as '.'."""
    if label == "hex":
        return "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in bytearray(seg))
    codec = dict(TEXT_ENCODINGS).get(label, "utf-8")
    return _printable(seg.decode(codec, "replace"))


def snippet(data, start, end, label, context=SNIPPET_CONTEXT):
    """The bytes around a match, readable, plus where it is and the matched bytes in hex:
    '...before match after...  [utf-16le @26: 68 00 69 00]'."""
    lo, hi = max(0, start - context), min(len(data), end + context)
    if label.startswith("utf-16"):          # keep whole code units on both sides
        lo = start - (start - lo) // 2 * 2
        hi = end + (hi - end) // 2 * 2
    text = render_bytes(data[lo:start], label) + render_bytes(data[start:end], label) + \
        render_bytes(data[end:hi], label)
    matched = bytearray(data[start:end])
    hexed = " ".join("%02x" % b for b in matched[:SNIPPET_HEX_MAX])
    if len(matched) > SNIPPET_HEX_MAX:
        hexed += " ..."
    return "%s%s%s  [%s @%d: %s]" % ("..." if lo > 0 else "", text, "..." if hi < len(data) else "",
                                      label, start, hexed)


def text_snippet(s, start, end, context=60):
    """A decoded string around a match: '...before match after...'."""
    lo, hi = max(0, start - context), min(len(s), end + context)
    return "%s%s%s" % ("..." if lo > 0 else "", _printable(s[lo:hi]), "..." if hi < len(s) else "")
