r"""A tiny XML DOM that writes files exactly like .NET's XmlSerializer.

Mods Studio 2 project files (.ms2) are produced by .NET's XmlSerializer:
2-space indentation, CRLF line endings, ``<Tag />`` for empty elements, an
``encoding="utf-16"`` declaration on UTF-8 text, and no trailing newline.
Reading and writing through this module round-trips such files byte for byte,
so Paintbooth only changes what the user actually edited.

:mod:`paintbooth.ms2` builds its project model on :class:`Elem`; nothing here
knows about MS2 semantics.

Text handling on write (see :func:`_esc` and :func:`_esc_attr`):

* every line break (``\r\n``, ``\r`` or ``\n``) in element text becomes ``\r\n``,
  like .NET's ``NewLineHandling.Replace``. Expat already turns the CRLFs of a file
  into ``\n`` on read, so an unchanged value is written back byte for byte;
* line breaks and tabs in attribute values are written as character references
  (``&#xD;``, ``&#xA;``, ``&#x9;``), which survive a re-read; a literal newline in an
  attribute would be normalised to a space by the next parser;
* characters that XML 1.0 forbids (most C0 controls, lone surrogates, U+FFFE/U+FFFF)
  are dropped. Writing them would produce a file that no parser, including
  Paintbooth's own, can open again.
"""

from __future__ import annotations

import copy
import re
import xml.parsers.expat
from collections.abc import Iterable, Mapping

DECLARATION = '<?xml version="1.0" encoding="utf-16"?>'

# Code points that are not allowed anywhere in an XML 1.0 document. Tab, LF and CR are allowed
# (and handled separately); everything else below U+0020, the surrogates and U+FFFE/U+FFFF is not.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_LINE_BREAK = re.compile("\r\n?")


class Elem:
    """One XML element: tag, attributes (in file order), children and text.

    An element has either children or text, never both (that is all .NET's serializer
    produces). ``text`` is ``None`` for an element with children, ``""`` for an element
    that was written as ``<Tag></Tag>`` / ``<Tag />``, and the string otherwise.

    The class has no ``__len__``/``__bool__`` on purpose: callers rely on every element
    being truthy (``el.find("X") or Elem("X")``).
    """

    __slots__ = ("attrs", "children", "tag", "text")

    def __init__(
        self,
        tag: str,
        text: str | None = None,
        attrs: Mapping[str, str] | None = None,
        children: Iterable[Elem] | None = None,
    ):
        self.tag = tag
        self.attrs: dict[str, str] = dict(attrs or {})
        self.children: list[Elem] = list(children or [])
        self.text = text

    # ---------------------------------------------------------- navigation
    def find(self, tag: str) -> Elem | None:
        """Return the first direct child named ``tag``, or None."""
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def findall(self, tag: str) -> list[Elem]:
        """Return every direct child named ``tag``, in document order."""
        return [c for c in self.children if c.tag == tag]

    def path(self, *tags: str) -> Elem | None:
        """Follow ``tags`` one level down each (``path("A", "B")`` is ``<A><B>``); None if one is missing."""
        e: Elem | None = self
        for t in tags:
            e = e.find(t) if e is not None else None
        return e

    def get_text(self, tag: str, default: str = "") -> str:
        """Return the text of child ``tag``, or ``default`` if the child is missing or has children."""
        e = self.find(tag)
        return default if e is None or e.text is None else e.text

    def set_text(self, tag: str, value: str | None, before: str | None = None) -> Elem:
        """Set a child's text, creating the child if it is missing.

        Args:
            tag: The child element's tag.
            value: The new text. Any children of that element are removed.
            before: Where a new child goes: in front of the first child with this tag, or
                at the end when ``before`` is None or not present. MS2 is picky about
                element order, so callers pass the tag that follows ``tag`` in MS2 files.

        Returns:
            The (possibly new) child element.
        """
        e = self.find(tag)
        if e is None:
            e = Elem(tag)
            idx = len(self.children)
            if before is not None:
                for i, c in enumerate(self.children):
                    if c.tag == before:
                        idx = i
                        break
            self.children.insert(idx, e)
        e.text = value
        e.children = []
        return e

    def type(self) -> str:
        """Return the ``xsi:type`` attribute (MS2's class name for the element), or ``""``."""
        return self.attrs.get("xsi:type", "")

    def clone(self) -> Elem:
        """Return a deep copy of this element and everything below it."""
        return copy.deepcopy(self)

    def __repr__(self) -> str:
        return f"<Elem {self.tag} {self.attrs} children={len(self.children)} text={self.text!r}>"


def _decode(data: bytes | str) -> str:
    """Return the document as text: UTF-16 when it has a UTF-16 BOM, otherwise UTF-8 (BOM optional)."""
    if isinstance(data, str):
        return data
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def parse(data: bytes | str) -> Elem:
    """Parse an XML document into an :class:`Elem` tree and return the root element.

    Args:
        data: The file contents. MS2 declares ``encoding="utf-16"`` but writes UTF-8 bytes,
            so the declaration is ignored and the encoding is taken from the BOM instead.

    Raises:
        xml.parsers.expat.ExpatError: The document is not well-formed.
    """
    text = _decode(data)
    # the declaration claims utf-16, but the bytes are UTF-8 text: drop it for the parser
    if text.startswith("<?xml"):
        text = text[text.index("?>") + 2 :]
    parser = xml.parsers.expat.ParserCreate()
    stack: list[Elem] = []
    root: list[Elem] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        e = Elem(tag, attrs=attrs)
        if stack:
            stack[-1].children.append(e)
        else:
            root.append(e)
        stack.append(e)

    def end(_tag: str) -> None:
        e = stack.pop()
        # whitespace between child elements is indentation, not text
        if e.children:
            e.text = None
        elif e.text is None:
            e.text = ""

    def chars(chunk: str) -> None:
        e = stack[-1]
        e.text = (e.text or "") + chunk

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.ordered_attributes = False
    parser.Parse(text, True)
    return root[0]


def _clean(s: str) -> str:
    """Drop characters XML 1.0 cannot represent (see the module docstring)."""
    return _INVALID_XML.sub("", s)


def _esc(s: str) -> str:
    """Escape element text the way .NET writes it (``&``, ``<``, ``>``; line breaks as CRLF)."""
    s = _LINE_BREAK.sub("\n", _clean(s))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "\r\n")


def _esc_attr(s: str) -> str:
    """Escape an attribute value: like :func:`_esc`, plus quotes and character references for breaks/tabs."""
    s = _clean(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return s.replace("\r", "&#xD;").replace("\n", "&#xA;").replace("\t", "&#x9;")


def serialize(root: Elem) -> bytes:
    """Write ``root`` the way .NET's XmlSerializer would, as UTF-8 bytes.

    Elements with children are written over several lines, elements with text on one
    line, and empty elements as ``<Tag />``. Lines are joined with CRLF and the file
    has no trailing newline, exactly like Mods Studio 2's own saves.
    """
    out: list[str] = [DECLARATION]

    def walk(e: Elem, depth: int) -> None:
        pad = "  " * depth
        attrs = "".join(f' {k}="{_esc_attr(v)}"' for k, v in e.attrs.items())
        if e.children:
            out.append(f"{pad}<{e.tag}{attrs}>")
            for c in e.children:
                walk(c, depth + 1)
            out.append(f"{pad}</{e.tag}>")
        elif e.text:
            out.append(f"{pad}<{e.tag}{attrs}>{_esc(e.text)}</{e.tag}>")
        else:
            out.append(f"{pad}<{e.tag}{attrs} />")

    walk(root, 0)
    return "\r\n".join(out).encode("utf-8")


def E(tag: str, *children: Elem, text: str | None = None, type: str | None = None) -> Elem:  # noqa: N802 (short builder name used throughout, reads like markup)
    """Build an element tree in one expression.

    Example: ``E("Value", E("string", text="a"))`` is ``<Value><string>a</string></Value>``.

    Args:
        tag: The element's tag.
        *children: Child elements. When given, ``text`` is ignored.
        text: The element's text (only used without children).
        type: Value for the ``xsi:type`` attribute, if any.
    """
    return Elem(tag, text if not children else None, {"xsi:type": type} if type else None, list(children))
