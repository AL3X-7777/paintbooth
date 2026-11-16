"""Vehicle library: editable XML definitions of trucks (cabins + paintable accessories).

A :class:`Vehicle` is what an Advanced Truck Skin needs to know about a truck or trailer:
its cabins/bodies (``suitable_for[]`` values) and paintable accessories (``acc_list[]``
values). :mod:`paintbooth.ms2` stores the selected vehicle inside each skin through
:func:`from_ms2` / :func:`apply_to_ms2`.

Definitions come from three places:
* XML files in the library folder (yours to edit - changes reload live),
* vehicles embedded in opened MS2 projects (MS2 stores the full definition
  of the selected truck inside every item),
* ``Tools > Vehicles > Scan`` of game or mod files.

Two file formats are read:

* Mods Studio 2 vehicle files (``<Variable xsi:type="ScsVehicleVariable">``), as MS2 keeps
  them in its own vehicle folder. Drop them into the library folder as they are.
* Paintbooth's shorter format::

    <vehicle id="freightliner.cascadia2019" game="ats" brand="Freightliner"
             name="Cascadia 2019" author="SCS Software" definition_by="you">
      <variant id="day.freightliner.cascadia2019.cabin" name="Day Cab"/>
      <accessory value="bumper.aero_pt" group="Bumper" name="Aero Paint"/>
    </vehicle>
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .model import Node, parse_xml
from .xmldom import E, Elem


@dataclass
class Variant:
    """A cabin (truck) or body (trailer): ``id`` is the value that goes into ``suitable_for[]``."""

    id: str
    name: str
    image: str | None = None
    description: str | None = None
    # Buses only: the cabin's paint job mask region, e.g. "pjm_at_0x2048_size_4096x2048"
    # (see paintbooth.coach). None for trucks and trailers, whose masks cover the whole UV space.
    mask: str | None = None
    # Buses only: the cabin's model (from its definition), needed for window and trim films.
    model: str | None = None


@dataclass
class Accessory:
    """A paintable accessory: ``value`` (``"<type>.<item>"``) is what goes into ``acc_list[]``."""

    value: str
    name: str
    group: str = ""
    description: str | None = None  # None = absent in MS2 data, "" = present but empty
    image: str | None = None


@dataclass
class Vehicle:
    """One vehicle definition from the library, a project or a scan."""

    id: str
    game: str = ""  # "ats" | "ets2" | ""
    brand: str = ""
    name: str = ""
    author: str = "SCS Software"
    definition_by: str = ""
    vehicle_type: str = "NewTrucks"
    image: str = ""
    variants: list[Variant] = field(default_factory=list)
    accessories: list[Accessory] = field(default_factory=list)
    sounds: list[tuple[str, str, str]] = field(default_factory=list)  # (group, text, value)
    vehicle_name: str = ""  # MS2 "VehicleName" exactly as read ("Brand|Model" or a plain name)
    key: str = ""  # unique key in the library (the id, or "id  [file name]" if several files share it)
    source: str = ""  # file path or "project"
    line: int = 0

    @property
    def display(self) -> str:
        """Return the label used in vehicle lists: ``"<author> - <name>"``."""
        return f"{self.author or 'Unknown'} - {self.name or self.id}"

    @property
    def is_trailer(self) -> bool:
        """True for ownable trailers (MS2 vehicle type ``Trailers``)."""
        return "trailer" in (self.vehicle_type or "").lower()

    @property
    def is_bus(self) -> bool:
        """True for coaches (vehicle type ``Buses``, from the ETS2 Coaches DLC)."""
        return "bus" in (self.vehicle_type or "").lower()

    @property
    def kind(self) -> str:
        """Return ``"trailer"``, ``"bus"`` or ``"truck"``."""
        return "trailer" if self.is_trailer else "bus" if self.is_bus else "truck"

    def fits_game(self, game: str) -> bool:
        """Return True if the vehicle can be used in ``game``; no game on either side matches any.

        MS2 marks some definitions "All" (both games).
        """
        return not self.game or not game or self.game in (game, "all")

    def same_content(self, other: Vehicle) -> bool:
        """Return True if both define the same vehicle (ignoring where they were loaded from)."""
        keys = ("id", "brand", "name", "author", "variants", "accessories")
        return all(getattr(self, k) == getattr(other, k) for k in keys)


def library_dir() -> Path:
    """Return the user's vehicle library folder."""
    from .appdirs import vehicles_dir

    return vehicles_dir()


# --------------------------------------------------------------------------
# Library XML
# --------------------------------------------------------------------------


def read_text(path: Path) -> tuple[str, str]:
    """Read an XML file in whatever encoding it really uses. Returns (text, encoding)."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text back in the encoding it was read with (atomic replace)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(
        text.replace("\r\n", "\n").replace("\n", "\r\n").encode(encoding, errors="replace")
        if encoding != "utf-16"
        else text.encode("utf-16")
    )
    os.replace(tmp, path)


def parse_text(text: str) -> Node:
    """Parse XML text whose declaration may lie about the encoding.

    MS2 writes ``encoding="utf-16"`` on UTF-8 files, so the declaration is blanked out
    (with spaces, so line and column numbers stay right).

    Raises:
        ValueError: ``(message, line)`` for malformed XML.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if text.lstrip().startswith("<?xml"):
        start = text.index("<?xml")
        end = text.index("?>", start) + 2
        text = text[:start] + " " * (end - start) + text[end:]
    return parse_xml(text)


def pretty(text: str) -> str:
    """SCS localisation keys -> readable text: '@@brand_kogel@@ @@series_kogel_cool@@' -> 'Kogel Cool'."""
    if "@@" not in (text or ""):
        return text
    seen: list[str] = []

    def word(m: re.Match[str]) -> str:
        key = m.group(1)
        key = re.sub(r"^(brand|series|name|model|trailer|truck|body|chassis|cabin)_", "", key)
        words = [w for w in key.split("_") if w]
        out = []
        for w in words:
            if w.lower() in seen:
                continue
            seen.append(w.lower())
            out.append(w[0].upper() + w[1:])
        return " ".join(out)

    return re.sub(r"\s+", " ", re.sub(r"@@([^@]+)@@", word, text)).strip()


def _node_text(n: Node, tag: str, default: str = "") -> str:
    """Return the stripped text of child ``tag`` of ``n``, or ``default``."""
    c = n.find(tag)
    return default if c is None else c.text.strip()


def _node_opt(n: Node, tag: str) -> str | None:
    """Return the stripped text of child ``tag``, or None if there is no such child."""
    c = n.find(tag)
    return None if c is None else c.text.strip()


def _node_kids(n: Node, tag: str) -> list[Node]:
    """Return the children of child ``tag`` of ``n`` (none if it is missing)."""
    c = n.find(tag)
    return c.children if c is not None else []


def from_ms2_node(n: Node, source: str = "") -> Vehicle:
    """Vehicle from a parsed MS2 vehicle file (``<Variable xsi:type="ScsVehicleVariable">``)."""
    vname = _node_text(n, "VehicleName")
    brand, _, model = vname.partition("|")
    v = Vehicle(
        _node_text(n, "InternalName"),
        _node_text(n, "Game").lower(),
        pretty(brand.strip()),
        pretty((model or brand).strip()),
        _node_text(n, "VehicleAuthor"),
        _node_text(n, "XmlDefinitionAuthor"),
        _node_text(n, "VehicleType", "NewTrucks") or "NewTrucks",
        _node_text(n, "VehicleImage"),
        source=source,
        line=n.line,
    )
    v.vehicle_name = vname
    if not model and v.brand.split():
        v.brand = v.brand.split()[0]  # "Kögel Cargo" (no "Brand|") -> listed under Kögel
    for x in _node_kids(n, "VehicleVariants"):
        v.variants.append(
            Variant(
                _node_text(x, "InternalName"),
                _node_text(x, "Name"),
                _node_opt(x, "Image"),
                _node_opt(x, "Description"),
            )
        )
    for x in _node_kids(n, "AccessoryItems"):
        v.accessories.append(
            Accessory(
                _node_text(x, "Value"),
                _node_text(x, "Text"),
                _node_text(x, "Group"),
                _node_opt(x, "Description"),
                _node_opt(x, "Image"),
            )
        )
    for x in _node_kids(n, "SoundItems"):
        v.sounds.append((_node_text(x, "Group"), _node_text(x, "Text"), _node_text(x, "Value")))
    return v


def is_ms2_vehicle(node: Node) -> bool:
    """Return True if ``node`` is the root of an MS2 vehicle file (an ScsVehicleVariable)."""
    return (
        node.tag == "Variable" and node.attrib.get("xsi:type", "ScsVehicleVariable") == "ScsVehicleVariable"
    )


class Library(dict[str, Vehicle]):
    """Vehicles by unique key.

    Several files may define the same internal name (ATS and ETS2 versions, or different
    authors' definitions); every file stays a separate entry.
    """

    def with_id(self, vid: str, game: str = "") -> list[Vehicle]:
        """Return every entry with internal name ``vid`` usable in ``game``."""
        return [v for v in self.values() if v.id == vid and v.fits_game(game)]

    def match(self, v: Vehicle, game: str = "") -> Vehicle | None:
        """The library entry that ``v`` (e.g. a definition embedded in a project) came from, if any."""
        cands = self.with_id(v.id, game or v.game)
        for c in cands:
            if c.same_content(v):
                return c
        return cands[0] if cands else None


def _skinyard_vehicle(n: Node, rel: str, source: str, errors: list[str]) -> Vehicle | None:
    """Return the vehicle of a ``<vehicle>`` element (Paintbooth format); add problems to ``errors``."""
    a = n.attrib
    vid = a.get("id", "").strip()
    if not vid:
        errors.append(f"{rel}:{n.line}: <vehicle> needs an id")
        return None
    v = Vehicle(
        vid,
        a.get("game", "").lower(),
        a.get("brand", ""),
        a.get("name", vid),
        a.get("author", "SCS Software"),
        a.get("definition_by", ""),
        a.get("type", "NewTrucks"),
        a.get("image", ""),
        source=source,
        line=n.line,
    )
    for c in n.find_all("variant"):
        if c.attrib.get("id"):
            v.variants.append(
                Variant(
                    c.attrib["id"],
                    c.attrib.get("name", c.attrib["id"]),
                    c.attrib.get("image"),
                    c.attrib.get("description"),
                    c.attrib.get("mask"),
                    c.attrib.get("model"),
                )
            )
    for c in n.find_all("accessory"):
        if c.attrib.get("value"):
            v.accessories.append(
                Accessory(
                    c.attrib["value"],
                    c.attrib.get("name", c.attrib["value"]),
                    c.attrib.get("group", ""),
                    c.attrib.get("description"),
                    c.attrib.get("image"),
                )
            )
    for c in n.find_all("sound"):
        v.sounds.append((c.attrib.get("group", ""), c.attrib.get("name", ""), c.attrib.get("value", "")))
    if not v.variants:
        errors.append(f"{rel}:{n.line}: vehicle {vid} has no <variant> entries")
    return v


def _load_file(f: Path, rel: str, errors: list[str]) -> list[Vehicle]:
    """Return the vehicles defined in library file ``f`` (either format); add problems to ``errors``."""
    try:
        root = parse_text(read_text(f)[0])
    except ValueError as e:
        errors.append(f"{rel}:{e.args[1]}: XML syntax error: {e.args[0]}")
        return []
    except OSError as e:
        errors.append(f"{rel}: {e.strerror}")
        return []
    if is_ms2_vehicle(root):
        v = from_ms2_node(root, str(f))
        if not v.id:
            errors.append(f"{rel}:{root.line}: <InternalName> of the vehicle is missing")
            return []
        if not v.variants:
            errors.append(f"{rel}:{root.line}: vehicle {v.id} has no <Variant> entries")
        return [v]
    nodes = [root] if root.tag == "vehicle" else root.find_all("vehicle")
    if not nodes:
        errors.append(f"{rel}:{root.line}: not a vehicle file (expected <vehicle> or an MS2 <Variable>)")
    found = [_skinyard_vehicle(n, rel, str(f), errors) for n in nodes]
    return [v for v in found if v is not None]


def load_library(folder: Path) -> tuple[Library, list[str]]:
    """Read every ``*.xml`` below ``folder`` into a :class:`Library`.

    A file may hold an MS2 vehicle, one ``<vehicle>`` or several inside another root.
    When several files define the same id, each entry's key gets the file name
    (``"id  [file.xml]"``) so none is lost.

    Returns:
        (library, problems as ``"file:line: message"``). A missing folder gives an empty library.
    """
    found: list[Vehicle] = []
    errors: list[str] = []
    vehicles = Library()
    if not folder.is_dir():
        return vehicles, errors
    for f in sorted(folder.rglob("*.xml")):
        found += _load_file(f, f.relative_to(folder).as_posix(), errors)
    found = _drop_generated_duplicates(found)
    counts: dict[str, int] = {}
    for v in found:
        counts[v.id] = counts.get(v.id, 0) + 1
    for v in found:
        key = v.id
        if counts[v.id] > 1:
            key = f"{v.id}  [{Path(v.source).name}]"
            n = 2
            while key in vehicles:
                key = f"{v.id}  [{Path(v.source).name} #{n}]"
                n += 1
        v.key = key
        vehicles[key] = v
    return vehicles, errors


#: Files Paintbooth writes itself, best first; a vehicle in one of them is hidden when a better
#: file (the user's own, or a scan of their game) defines the same id.
_GENERATED = {"Paintbooth scan": 1, "Paintbooth library": 2}


def _drop_generated_duplicates(found: list[Vehicle]) -> list[Vehicle]:
    """Keep one copy of vehicles that Paintbooth wrote more than once.

    The library, a game scan and the coach auto-scan can each write the same vehicle. Per id,
    only the best kind is kept: the user's own files, else the scan, else the library. Several
    user files with one id are all kept (they are told apart by file name).
    """
    best: dict[str, int] = {}
    for v in found:
        rank = _GENERATED.get(v.definition_by, 0)
        best[v.id] = min(best.get(v.id, rank), rank)
    out, seen = [], set()
    for v in found:
        rank = _GENERATED.get(v.definition_by, 0)
        if rank != best[v.id] or (rank and v.id in seen):
            continue
        seen.add(v.id)
        out.append(v)
    return out


def to_xml(v: Vehicle, comment: str = "") -> str:
    """Return ``v`` in Paintbooth's short vehicle XML format, with an optional comment line."""

    def attrs(**kw: Any) -> str:
        # empty values are left out, except description/image, where "" differs from absent
        return "".join(
            f" {k}={quoteattr(str(val))}"
            for k, val in kw.items()
            if val is not None and (val or k in ("description", "image"))
        )

    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    if comment:
        lines.append(f"<!-- {escape(comment)} -->")
    head = attrs(
        id=v.id,
        game=v.game,
        brand=v.brand,
        name=v.name,
        author=v.author,
        definition_by=v.definition_by,
        image=v.image or None,
        type=v.vehicle_type if v.vehicle_type != "NewTrucks" else None,
    )
    lines.append(f"<vehicle{head}>")
    lines.append("  <!-- cabins: id is what goes into suitable_for[] -->")
    for x in v.variants:
        var = attrs(
            id=x.id, name=x.name, image=x.image, description=x.description, mask=x.mask, model=x.model
        )
        lines.append(f"  <variant{var}/>")
    lines.append("  <!-- paintable accessories: value is what goes into acc_list[] -->")
    for a in v.accessories:
        acc = attrs(value=a.value, group=a.group, name=a.name, description=a.description, image=a.image)
        lines.append(f"  <accessory{acc}/>")
    for g, t, val in v.sounds:
        lines.append(f"  <sound{attrs(group=g, name=t, value=val)}/>")
    lines.append("</vehicle>")
    return "\n".join(lines) + "\n"


def save_to_library(v: Vehicle, folder: Path, comment: str = "") -> Path:
    """Write ``v`` to ``folder/<id>.xml`` (replacing that file) and return the path."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (re.sub(r"[^\w.-]", "_", v.id) + ".xml")
    p.write_text(to_xml(v, comment), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# MS2 ScsVehicleVariable <-> Vehicle
# --------------------------------------------------------------------------


def _opt(e: Elem, tag: str) -> str | None:
    """Return the text of child ``tag``, ``""`` if it is empty, None if it is absent."""
    c = e.find(tag)
    return None if c is None else (c.text or "")


def from_ms2(var: Elem) -> Vehicle:
    """Return the vehicle stored in a project's ScsVehicleVariable element."""
    vname = var.get_text("VehicleName")
    brand, _, model = vname.partition("|")
    v = Vehicle(
        var.get_text("InternalName"),
        var.get_text("Game").lower(),
        pretty(brand.strip()),
        pretty((model or brand).strip()),
        var.get_text("VehicleAuthor"),
        var.get_text("XmlDefinitionAuthor"),
        var.get_text("VehicleType", "NewTrucks"),
        var.get_text("VehicleImage"),
        source="project",
    )
    v.vehicle_name = vname
    if not model and v.brand.split():
        v.brand = v.brand.split()[0]
    for x in (var.find("VehicleVariants") or Elem("x")).children:
        v.variants.append(
            Variant(
                x.get_text("InternalName"),
                x.get_text("Name"),
                _opt(x, "Image"),
                _opt(x, "Description"),
                _opt(x, "PaintboothMask"),  # bus data only Paintbooth writes (see apply_to_ms2)
                _opt(x, "PaintboothModel"),
            )
        )
    for x in (var.find("AccessoryItems") or Elem("x")).children:
        v.accessories.append(
            Accessory(
                x.get_text("Value"),
                x.get_text("Text"),
                x.get_text("Group"),
                _opt(x, "Description"),
                _opt(x, "Image"),
            )
        )
    for x in (var.find("SoundItems") or Elem("x")).children:
        v.sounds.append((x.get_text("Group"), x.get_text("Text"), x.get_text("Value")))
    return v


def apply_to_ms2(var: Elem, v: Vehicle) -> None:
    """Write vehicle ``v`` into an MS2 ScsVehicleVariable element (field order as MS2 writes it)."""

    def opt(tag: str, val: str | None) -> list[Elem]:
        return [] if val is None else [E(tag, text=val)]

    acc = E(
        "AccessoryItems",
        *[
            E(
                "ListItem",
                *opt("Description", a.description),
                E("Group", text=a.group),
                *opt("Image", a.image),
                E("Text", text=a.name),
                E("Value", text=a.value),
            )
            for a in v.accessories
        ],
    )
    sounds = E(
        "SoundItems",
        *[
            E("ListItem", E("Group", text=g), E("Text", text=t), E("Value", text=val))
            for g, t, val in (v.sounds or [("SCS Software", "Default sound", "sound")])
        ],
    )
    variants = E(
        "VehicleVariants",
        *[
            E(
                "Variant",
                E("AccessoryDependencies", text=""),
                *opt("Description", x.description),
                *opt("Image", x.image),
                E("InternalName", text=x.id),
                E("Name", text=x.name),
                # buses: extra elements Mods Studio 2 does not know; it skips them when loading
                *opt("PaintboothMask", x.mask),
                *opt("PaintboothModel", x.model),
            )
            for x in v.variants
        ],
    )
    fields = {
        "AccessoryItems": acc,
        "XmlSource": E("XmlSource", text="All"),
        "Game": E("Game", text=(v.game or "ats").upper() if v.game != "ets2" else "ETS2"),
        "InternalName": E("InternalName", text=v.id),
        "SoundItems": sounds,
        "VehicleAuthor": E("VehicleAuthor", text=v.author),
        "VehicleImage": E("VehicleImage", text=v.image),
        "VehicleName": E(
            "VehicleName", text=v.vehicle_name or (f"{v.brand}|{v.name}" if v.brand else v.name)
        ),
        "VehicleType": E("VehicleType", text=v.vehicle_type or "NewTrucks"),
        "VehicleVariants": variants,
        "XmlDefinitionAuthor": E("XmlDefinitionAuthor", text=v.definition_by),
        "DateUpdated": var.find("DateUpdated") or Elem("DateUpdated", "", {"xsi:nil": "true"}),
    }
    keep = [c for c in var.children if c.tag not in fields]
    var.children = keep + list(fields.values())


# --------------------------------------------------------------------------
# Text edits for the XML editor ("Add accessory", "Add cabin / body")
# --------------------------------------------------------------------------
# These work on the text itself, so the user's own formatting, comments and
# line endings stay as they are, and the edit can be undone in the editor.

from xml.sax.saxutils import escape as _esc  # noqa: E402

_ITEM_RE = re.compile(r"<ListItem\b[^>]*>(.*?)</ListItem>", re.S)
_TAG_RE = r"<{0}>(.*?)</{0}>"


@dataclass
class TextEdit:
    """Replace lines [start, end) (1-based, end exclusive) with ``text``."""

    start: int
    end: int
    text: str
    select_line: int  # line to show/select after the edit


def _lines(text: str) -> list[str]:
    """Split text into lines at LF; a CR stays at the line end, so CRLF files keep their line ends."""
    return text.split("\n")


def _indent_of(line: str) -> str:
    """Return the leading spaces and tabs of ``line``."""
    return line[: len(line) - len(line.lstrip(" \t"))]


def _unit_of(indent: str, parent_indent: str) -> str:
    """Return one indentation step: what ``indent`` adds to ``parent_indent`` (two spaces if nothing)."""
    extra = indent[len(parent_indent) :] if indent.startswith(parent_indent) else ""
    return extra or "  "


def xml_format(text: str) -> str:
    """Return ``"ms2"`` for an MS2 vehicle file, ``"paintbooth"`` for Paintbooth's short format."""
    return "ms2" if re.search(r"<Variable\b", text) else "paintbooth"


def accessory_groups(text: str) -> list[str]:
    """Group names used in the file, in first-seen order."""
    out: list[str] = []
    if xml_format(text) == "ms2":
        found = [
            re.search(_TAG_RE.format("Group"), m.group(1), re.S)
            for m in _ITEM_RE.finditer(_block(text, "AccessoryItems"))
        ]
        names = [g.group(1).strip() for g in found if g]
    else:
        names = re.findall(r"<accessory\b[^>]*\bgroup=\"([^\"]*)\"", text)
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def accessory_values(text: str) -> set[str]:
    """Return the accessory values (``acc_list[]`` entries) defined in the file."""
    if xml_format(text) == "ms2":
        return {
            m.group(1).strip()
            for m in re.finditer(_TAG_RE.format("Value"), _block(text, "AccessoryItems"), re.S)
        }
    return set(re.findall(r"<accessory\b[^>]*\bvalue=\"([^\"]*)\"", text))


def _block(text: str, tag: str) -> str:
    """Return the inner text of the first ``<tag>...</tag>``, or ``""``."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1) if m else ""


def _line_of(text: str, offset: int) -> int:
    """Return the 1-based line number of character ``offset``."""
    return text.count("\n", 0, offset) + 1


def _insert_into_list(
    text: str, list_tag: str, item_tag: str, children: list[tuple[str, str]], group: str | None
) -> TextEdit:
    """Return the edit that adds ``<item_tag>`` with ``children`` to ``<list_tag>`` (MS2 format).

    The item goes after the last item of the same ``group`` (or at the end), indented
    like the existing items. An empty or missing list element is created.

    Raises:
        ValueError: The file has neither the list nor a ``</Variable>`` to add it to.
    """
    lines = _lines(text)
    m = re.search(rf"<{list_tag}>(.*?)</{list_tag}>", text, re.S)
    if m is None:
        m_empty = re.search(rf"<{list_tag}\s*/>", text)
        if m_empty is None:
            root = re.search(r"</Variable>", text)
            if root is None:
                raise ValueError(f"Can't find <{list_tag}> in this file")
            ln = _line_of(text, root.start())
            p_ind = _indent_of(lines[ln - 1]) + "  "
            u = "  "
            body = _render_item(item_tag, children, p_ind + u, u)
            return TextEdit(ln, ln, f"{p_ind}<{list_tag}>\n{body}{p_ind}</{list_tag}>\n", ln + 1)
        ln = _line_of(text, m_empty.start())
        p_ind = _indent_of(lines[ln - 1])
        u = "  "
        body = _render_item(item_tag, children, p_ind + u, u)
        return TextEdit(ln, ln + 1, f"{p_ind}<{list_tag}>\n{body}{p_ind}</{list_tag}>\n", ln + 1)

    inner_start = m.start(1)
    items = list(re.finditer(rf"<{item_tag}\b[^>]*>(.*?)</{item_tag}>", m.group(1), re.S))
    close_line = _line_of(text, m.end(1))
    list_line = _line_of(text, m.start())
    p_ind = _indent_of(lines[list_line - 1])
    if items:
        first_line = _line_of(text, inner_start + items[0].start())
        i_ind = _indent_of(lines[first_line - 1])
        child = re.search(r"\n([ \t]*)<", items[0].group(1))
        u = _unit_of(child.group(1), i_ind) if child else "  "
    else:
        u = "  "
        i_ind = p_ind + u
    after = None
    if group is not None:
        for it in items:
            g = re.search(_TAG_RE.format("Group"), it.group(1), re.S)
            if g and g.group(1).strip() == group:
                after = it
    if after is None and items:
        after = items[-1]
    ins = _line_of(text, inner_start + after.end()) + 1 if after else close_line
    body = _render_item(item_tag, children, i_ind, u)
    return TextEdit(ins, ins, body, ins + 1)


def _render_item(tag: str, children: list[tuple[str, str]], ind: str, unit: str) -> str:
    """Return ``<tag>`` with one ``<k>v</k>`` line per child, indented by ``ind`` and ``unit``."""
    out = [f"{ind}<{tag}>"]
    for k, v in children:
        out.append(f"{ind}{unit}<{k}>{_esc(v)}</{k}>")
    out.append(f"{ind}</{tag}>")
    return "\n".join(out) + "\n"


def _insert_skinyard(text: str, tag: str, attrs: list[tuple[str, str]], group: str | None) -> TextEdit:
    """Return the edit that adds ``<tag .../>`` (short format) after the last one of the same group.

    Raises:
        ValueError: The file has no ``</vehicle>``.
    """
    lines = _lines(text)
    same, last = None, None
    for i, line in enumerate(lines, 1):
        if re.search(rf"<{tag}\b", line):
            last = i
            if group is not None and re.search(r'\bgroup="' + re.escape(_esc(group)) + '"', line):
                same = i
    at = same or last
    attr_s = "".join(f" {k}={quoteattr(v)}" for k, v in attrs if v)
    if at:
        ind = _indent_of(lines[at - 1])
        return TextEdit(at + 1, at + 1, f"{ind}<{tag}{attr_s}/>\n", at + 1)
    close = next((i for i, line in enumerate(lines, 1) if "</vehicle>" in line), None)
    if close is None:
        raise ValueError("Can't find </vehicle> in this file")
    ind = _indent_of(lines[close - 1]) + "  "
    return TextEdit(close, close, f"{ind}<{tag}{attr_s}/>\n", close)


def add_accessory_edit(text: str, name: str, value: str, group: str, description: str = "") -> TextEdit:
    """Return the edit that adds an accessory to a vehicle file of either format.

    Raises:
        ValueError: The file's structure is not recognised.
    """
    if xml_format(text) == "ms2":
        ch = ([("Description", description)] if description else []) + [
            ("Group", group),
            ("Text", name),
            ("Value", value),
        ]
        return _insert_into_list(text, "AccessoryItems", "ListItem", ch, group)
    return _insert_skinyard(
        text,
        "accessory",
        [("value", value), ("group", group), ("name", name), ("description", description)],
        group,
    )


def add_variant_edit(text: str, name: str, internal: str, description: str = "") -> TextEdit:
    """Return the edit that adds a cabin/body to a vehicle file of either format.

    Raises:
        ValueError: The file's structure is not recognised.
    """
    if xml_format(text) == "ms2":
        ch = [("InternalName", internal), ("Name", name)] + (
            [("Description", description)] if description else []
        )
        return _insert_into_list(text, "VehicleVariants", "Variant", ch, None)
    return _insert_skinyard(text, "variant", [("id", internal), ("name", name)], None)


def apply_edit(text: str, e: TextEdit) -> str:
    """Return ``text`` with edit ``e`` applied."""
    lines = text.split("\n")
    new = e.text[:-1].split("\n") if e.text.endswith("\n") else e.text.split("\n")
    return "\n".join(lines[: e.start - 1] + new + lines[e.end - 1 :])
