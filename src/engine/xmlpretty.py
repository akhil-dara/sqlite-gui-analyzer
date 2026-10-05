"""Pretty-printing of XML text with a small, strict, iterative parser of its own.

Nothing is expanded or fetched: the only document type declaration accepted is a plain
<!DOCTYPE name [SYSTEM/PUBLIC id]> in the prolog, kept as written and never read; one with
an internal subset, any <!ENTITY, or a DOCTYPE anywhere else is refused. Only the five
predefined entities and numeric character references are accepted, and every reference is
written back exactly as it appeared. Depth and node count are bounded, the work is linear
in the length of the text and nothing recurses, so hostile input fails fast with a ValueError
(the only exception pretty_xml raises for bad input).
"""

import re

__all__ = ["pretty_xml", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_NODES", "DTD_REFUSED"]

DEFAULT_MAX_DEPTH = 256
DEFAULT_MAX_NODES = 1000000
DTD_REFUSED = "XML with a DTD is not pretty-printed"

# XML 1.0 (fifth edition) NameStartChar and NameChar.
_START = (r":A-Z_a-zÀ-ÖØ-öø-˿Ͱ-ͽͿ-῿"
          r"‌-‍⁰-↏Ⰰ-⿯、-퟿豈-﷏ﷰ-�"
          r"\U00010000-\U000effff")
_CHARS = _START + r"\-.0-9·̀-ͯ‿-⁀"

# Every pattern is a single character-class run (or a fixed alternation of such runs), so a
# match costs at most one pass over the characters it looks at: no catastrophic backtracking.
_NAME = re.compile("[%s][%s]*" % (_START, _CHARS))
_WS = re.compile(r"[ \t\r\n]*")
_REF = re.compile("&(?:#([0-9]+)|#x([0-9a-fA-F]+)|([%s][%s]*));" % (_START, _CHARS))
_BAD_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_ENTITY_DECL = re.compile(r"<!entity", re.IGNORECASE)
_DOCTYPE_ANY = re.compile(r"<!doctype", re.IGNORECASE)
# The only document type declaration accepted: a name and an optional external ID, with no
# internal subset. It is kept as written and never fetched or read.
_SYSTEM_LIT = r"(?:\"[^\"]*\"|'[^']*')"
_PUBID_LIT = r"(?:\"[ \r\na-zA-Z0-9\-'()+,./:=?;!*#@$_%]*\"|'[ \r\na-zA-Z0-9\-()+,./:=?;!*#@$_%]*')"
_DOCTYPE = re.compile(
    r"<!DOCTYPE[ \t\r\n]+[%s][%s]*" % (_START, _CHARS)
    + r"(?:[ \t\r\n]+(?:SYSTEM[ \t\r\n]+" + _SYSTEM_LIT
    + r"|PUBLIC[ \t\r\n]+" + _PUBID_LIT + r"[ \t\r\n]+" + _SYSTEM_LIT + r"))?"
    + r"[ \t\r\n]*>")
_DECL = re.compile(
    r"<\?xml[ \t\r\n]+version[ \t\r\n]*=[ \t\r\n]*(?:\"1\.[0-9]+\"|'1\.[0-9]+')"
    r"(?:[ \t\r\n]+encoding[ \t\r\n]*=[ \t\r\n]*"
    r"(?:\"[A-Za-z][A-Za-z0-9._\-]*\"|'[A-Za-z][A-Za-z0-9._\-]*'))?"
    r"(?:[ \t\r\n]+standalone[ \t\r\n]*=[ \t\r\n]*(?:\"(?:yes|no)\"|'(?:yes|no)'))?"
    r"[ \t\r\n]*\?>")

_XML_WS = " \t\r\n"
_PREDEFINED = frozenset(("lt", "gt", "amp", "apos", "quot"))


def pretty_xml(text, indent="  ", max_depth=DEFAULT_MAX_DEPTH, max_nodes=DEFAULT_MAX_NODES):
    """The XML in `text` laid out one element per line, children indented by `indent`.

    Text-only elements stay on one line, whitespace between elements is dropped, other text
    is stripped and put on its own line, comments, CDATA sections and processing instructions
    are kept verbatim, attributes are re-written as name="value" with the value as written.
    Raises ValueError for anything that is not well-formed XML, for any DTD, and when the
    document is deeper than `max_depth` elements or has more than `max_nodes` nodes.
    """
    if not isinstance(text, str):
        raise ValueError("not XML: the value is not text")
    if not isinstance(indent, str) or indent.strip(" \t"):
        raise ValueError("indent must be made of spaces or tabs")
    for value, label in ((max_depth, "max_depth"), (max_nodes, "max_nodes")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("%s must be a whole number of at least 1" % label)
    decl, top = _parse(text, max_depth, max_nodes)
    return _emit(decl, top, indent)


def _fail(text, pos, reason):
    line = text.count("\n", 0, pos) + 1
    column = pos - text.rfind("\n", 0, pos)
    raise ValueError("not XML: %s at line %d column %d" % (reason, line, column))


def _where(text, pos):
    return "line %d column %d" % (text.count("\n", 0, pos) + 1, pos - text.rfind("\n", 0, pos))


def _is_xml_char(cp):
    return (cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD
            or 0x10000 <= cp <= 0x10FFFF)


def _check_refs(text, start, end):
    """Validate every '&' reference in text[start:end]; nothing is expanded."""
    k = text.find("&", start, end)
    while k >= 0:
        m = _REF.match(text, k, end)
        if m is None:
            _fail(text, k, "'&' does not start an entity or character reference")
        dec, hexa, name = m.groups()
        if name is not None:
            if name not in _PREDEFINED:
                shown = name if len(name) <= 40 else name[:40] + "..."
                _fail(text, k, "unknown entity &%s; (only &lt; &gt; &amp; &apos; &quot; and "
                               "character references are allowed)" % shown)
        else:
            digits = (dec if dec is not None else hexa).lstrip("0")
            if len(digits) > 8 or not _is_xml_char(
                    int(digits or "0", 10 if dec is not None else 16)):
                _fail(text, k, "character reference to a character not allowed in XML")
        k = text.find("&", m.end(), end)


def _parse(text, max_depth, max_nodes):
    """(XML declaration or None, top-level nodes). An element is a list
    [open tag without its closing '>', name, self-closing, children]; anything else is a
    tuple (kind, text) with kind 'text', 'cdata', 'comment', 'pi' or 'doctype'."""
    if _ENTITY_DECL.search(text) is not None:
        raise ValueError(DTD_REFUSED)
    # At most one "<!doctype" (in any case) may appear in the whole text, and it must turn
    # out to be the plain declaration in the prolog; text that merely contains it (inside a
    # comment, CDATA or a processing instruction) is refused all the same.
    m = _DOCTYPE_ANY.search(text)
    doctype_at = -1
    if m is not None:
        doctype_at = m.start()
        if _DOCTYPE_ANY.search(text, m.end()) is not None:
            raise ValueError(DTD_REFUSED)
    doctype_done = False
    m = _BAD_CHAR.search(text)
    if m is not None:
        _fail(text, m.start(), "character U+%04X is not allowed in XML" % ord(m.group()))

    n = len(text)
    start = 1 if text.startswith("﻿") else 0
    i = start
    decl = None
    top = []
    stack = []              # [element, position of its start tag]
    root_done = False
    count = 0

    def add(node):
        if count > max_nodes:
            raise ValueError("XML has more than %s nodes (the node limit)"
                             % format(max_nodes, ","))
        if stack:
            stack[-1][0][3].append(node)
        else:
            top.append(node)

    while i < n:
        if text[i] != "<":
            j = text.find("<", i)
            if j < 0:
                j = n
            if stack:
                bad = text.find("]]>", i, j)
                if bad >= 0:
                    _fail(text, bad, "']]>' in text")
                _check_refs(text, i, j)
                count += 1
                add(("text", text[i:j]))
            else:
                k = _WS.match(text, i, j).end()
                if k < j:
                    _fail(text, k, "text outside the root element")
            i = j
            continue

        if text.startswith("<?", i):
            end = text.find("?>", i + 2)
            if end < 0:
                _fail(text, i, "unterminated processing instruction")
            m = _NAME.match(text, i + 2, end)
            if m is None:
                _fail(text, i + 2, "processing instruction without a target name")
            k = m.end()
            if k < end and text[k] not in _XML_WS:
                _fail(text, k, "bad processing instruction target")
            target = m.group()
            raw = text[i:end + 2]
            if target.lower() == "xml":
                if target != "xml":
                    _fail(text, i, "processing instruction target %r is reserved" % target)
                if i != start:
                    _fail(text, i, "XML declaration not at the start of the document")
                if _DECL.fullmatch(raw) is None:
                    _fail(text, i, "malformed XML declaration")
                decl = raw
            else:
                count += 1
                add(("pi", raw))
            i = end + 2
            continue

        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end < 0:
                _fail(text, i, "unterminated comment")
            dash = text.find("--", i + 4, end)
            if dash >= 0:
                _fail(text, dash, "'--' inside a comment")
            if end > i + 4 and text[end - 1] == "-":
                _fail(text, end - 1, "comment ending in '--->'")
            count += 1
            add(("comment", text[i:end + 3]))
            i = end + 3
            continue

        if text.startswith("<![CDATA[", i):
            if not stack:
                _fail(text, i, "CDATA section outside the root element")
            end = text.find("]]>", i + 9)
            if end < 0:
                _fail(text, i, "unterminated CDATA section")
            count += 1
            add(("cdata", text[i:end + 3]))
            i = end + 3
            continue

        if i == doctype_at:
            if stack or root_done:
                raise ValueError(DTD_REFUSED)
            m = _DOCTYPE.match(text, i)
            if m is None:
                raise ValueError(DTD_REFUSED)
            count += 1
            add(("doctype", m.group()))
            doctype_done = True
            i = m.end()
            continue

        if text.startswith("<!", i):
            _fail(text, i, "markup declaration '<!' is not allowed here")

        if text.startswith("</", i):
            m = _NAME.match(text, i + 2)
            if m is None:
                _fail(text, i + 2, "bad end tag name")
            k = _WS.match(text, m.end()).end()
            if k >= n:
                _fail(text, i, "unterminated end tag")
            if text[k] != ">":
                _fail(text, k, "end tag not closed with '>'")
            name = m.group()
            if not stack:
                _fail(text, i, "end tag </%s> without a start tag" % name)
            elem, opened = stack.pop()
            if elem[1] != name:
                _fail(text, i, "end tag </%s> does not match <%s> opened at %s"
                      % (name, elem[1], _where(text, opened)))
            if not stack:
                root_done = True
            i = k + 1
            continue

        # start tag
        if root_done:
            _fail(text, i, "more than one root element")
        m = _NAME.match(text, i + 1)
        if m is None:
            _fail(text, i, "'<' not followed by a tag name")
        name = m.group()
        parts = ["<", name]
        seen = set()
        j = m.end()
        while True:
            k = _WS.match(text, j).end()
            if k >= n:
                _fail(text, i, "unterminated start tag <%s>" % name)
            c = text[k]
            if c == ">":
                closed = False
                j = k + 1
                break
            if c == "/":
                if text.startswith("/>", k):
                    closed = True
                    j = k + 2
                    break
                _fail(text, k, "'/' not followed by '>' in a start tag")
            if k == j:
                _fail(text, k, "expected whitespace, '>' or '/>' in a start tag")
            am = _NAME.match(text, k)
            if am is None:
                _fail(text, k, "bad attribute name")
            aname = am.group()
            if aname in seen:
                _fail(text, k, "duplicate attribute %s" % aname)
            seen.add(aname)
            k = _WS.match(text, am.end()).end()
            if text[k:k + 1] != "=":
                _fail(text, k, "expected '=' after attribute %s" % aname)
            k = _WS.match(text, k + 1).end()
            q = text[k:k + 1]
            if q != '"' and q != "'":
                _fail(text, k, "value of attribute %s is not quoted" % aname)
            e = text.find(q, k + 1)
            if e < 0:
                _fail(text, k, "unterminated value of attribute %s" % aname)
            lt = text.find("<", k + 1, e)
            if lt >= 0:
                _fail(text, lt, "'<' in the value of attribute %s" % aname)
            _check_refs(text, k + 1, e)
            value = text[k + 1:e]
            if q == "'":
                value = value.replace('"', "&quot;")
            parts.append(' %s="%s"' % (aname, value))
            j = e + 1

        if len(stack) + 1 > max_depth:
            raise ValueError("XML is nested deeper than %s elements (the depth limit)"
                             % format(max_depth, ","))
        count += 1
        elem = ["".join(parts), name, closed, []]
        add(elem)
        if closed:
            if not stack:
                root_done = True
        else:
            stack.append((elem, i))
        i = j

    if doctype_at >= 0 and not doctype_done:
        raise ValueError(DTD_REFUSED)
    if stack:
        elem, opened = stack[-1]
        _fail(text, opened, "element <%s> is not closed" % elem[1])
    if not root_done:
        _fail(text, n, "no root element")
    return decl, top


def _emit(decl, top, indent):
    out = []
    if decl is not None:
        out.append(decl)
    stack = [(node, 0) for node in reversed(top)]
    while stack:
        node, depth = stack.pop()
        pad = indent * depth
        if isinstance(node, str):                       # an end tag
            out.append(pad + node)
            continue
        if isinstance(node, tuple):
            kind, raw = node
            if kind == "text":
                raw = raw.strip(_XML_WS)
                if not raw:
                    continue
            out.append(pad + raw)
            continue
        opentag, name, closed, kids = node
        if closed:
            out.append(pad + opentag + "/>")
            continue
        inline = True
        for kid in kids:
            if not isinstance(kid, tuple) or kid[0] not in ("text", "cdata"):
                inline = False
                break
        if inline:
            out.append(pad + opentag + ">" + "".join(kid[1] for kid in kids)
                       + "</" + name + ">")
            continue
        out.append(pad + opentag + ">")
        stack.append(("</" + name + ">", depth))
        for kid in reversed(kids):
            stack.append((kid, depth + 1))
    return "\n".join(out)
