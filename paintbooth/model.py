"""Project and vehicle definitions of the ``project.xml`` format, loaded from plain XML files.

Everything Paintbooth knows about such a project lives in editable XML:

* ``project.xml``      - mod info and the list of skins
* ``vehicles/*.xml``   - one or more vehicle definitions per file

This is the format behind the ``paintbooth new/check/build/watch/ui`` commands (see
:mod:`paintbooth.builder` for the export). The GUI's own .pb/.ms2 projects live in
:mod:`paintbooth.ms2`.

Loading never raises for content problems: every problem becomes an
:class:`Issue` with file and line number, so the files can be edited while
the project is open and errors are reported instead of crashing.
"""

from __future__ import annotations

import math
import re
import xml.parsers.expat
from dataclasses import dataclass, field
from pathlib import Path

from .names import TOKEN_RE as _TOKEN_RE
from .util import srgb_to_linear

__all__ = [
    "COLOR_KEYS",
    "GAMES",
    "IMAGE_EXTS",
    "KINDS",
    "MODES",
    "TOKEN_RE",
    "Issue",
    "ModInfo",
    "Node",
    "Project",
    "Skin",
    "Slot",
    "Target",
    "Vehicle",
    "load_project",
    "load_vehicle_file",
    "parse_color",
    "parse_xml",
    "srgb_to_linear",  # kept here for older callers; lives in paintbooth.util
]

TOKEN_RE = _TOKEN_RE  # an SCS token (the game's limit for unit names); one rule in paintbooth.names
KINDS = {"truck", "trailer_owned"}
GAMES = {"ets2", "ats"}
COLOR_KEYS = ("base_color", "mask_r_color", "mask_g_color", "mask_b_color", "flip_color", "flake_color")
MODES = {"airbrush", "colormask", "plain"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".psd", ".tif", ".tiff", ".webp", ".dds"}
_RAW_KEY_RE = re.compile(r"[a-z_][a-z0-9_]*(\[\d*\])?")  # an SII attribute name, optionally an array


# --------------------------------------------------------------------------
# Tiny XML tree with line numbers
# --------------------------------------------------------------------------


@dataclass
class Node:
    """An XML element with the line it starts on (for error messages).

    Attributes:
        tag: Element name.
        attrib: Attributes.
        line: 1-based line number of the start tag.
        children: Child elements, in order.
        text: All character data directly inside the element, concatenated.
    """

    tag: str
    attrib: dict[str, str]
    line: int
    children: list[Node] = field(default_factory=list)
    text: str = ""

    def find_all(self, tag: str) -> list[Node]:
        """All direct children named ``tag``."""
        return [c for c in self.children if c.tag == tag]

    def find(self, tag: str) -> Node | None:
        """The first direct child named ``tag``, or None."""
        for c in self.children:
            if c.tag == tag:
                return c
        return None


def parse_xml(text: str) -> Node:
    """Parse XML, keeping element line numbers.

    Raises:
        ValueError: With ``args == (message, line)`` for a syntax error.
    """
    parser = xml.parsers.expat.ParserCreate()
    stack: list[Node] = []
    root: list[Node] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        node = Node(tag, dict(attrs), parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(_tag: str) -> None:
        stack.pop()

    def chars(data: str) -> None:
        if stack:
            stack[-1].text += data

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(text, True)
    except xml.parsers.expat.ExpatError as e:
        raise ValueError(xml.parsers.expat.ErrorString(e.code), e.lineno) from None
    return root[0]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Issue:
    """A problem found while loading or building, shown as ``LEVEL: file:line: message``.

    Attributes:
        level: "error" (blocks the build) or "warning".
        file: The file, relative to the project root where possible.
        line: 1-based line, or 0 when the problem is not tied to a line.
        message: What is wrong, for the user.
    """

    level: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.level.upper()}: {loc}: {self.message}"


@dataclass
class Slot:
    """A texture slot of a vehicle: the main paint job mask, or one for a set of accessories.

    Attributes:
        id: Slot name (an SCS token); names the mask file.
        main: True for the main paint job (exactly one per vehicle).
        accs: Accessory unit names the slot's mask is applied to (non-main slots).
    """

    id: str
    main: bool
    accs: list[str]


@dataclass
class Vehicle:
    """A truck or owned trailer that skins can target (from ``vehicles/*.xml``).

    Attributes:
        id: Internal name, e.g. ``scania.s_2016``.
        kind: "truck" or "trailer_owned" (the game's folder names).
        name: Display name.
        game: "ets2", "ats" or "" (any).
        template: Expected mask size, or None to accept any power-of-two size.
        slots: Slot id -> slot.
        file: The defining file, relative to the project.
        line: Line of the ``<vehicle>`` element.
    """

    id: str
    kind: str
    name: str
    game: str
    template: tuple[int, int] | None
    slots: dict[str, Slot]
    file: str
    line: int

    @property
    def folder(self) -> str:
        """Folder name for this vehicle's masks (dots become ``_``).

        Not injective (``a.b_c`` and ``a_b.c`` share a folder); the builder reports the clash.
        It is kept as it is because changing it would rename the files of every existing mod.
        """
        return self.id.replace(".", "_")

    @property
    def main_slot(self) -> Slot:
        """The slot with ``main=True`` (the loader guarantees there is exactly one)."""
        return next(s for s in self.slots.values() if s.main)


@dataclass
class Target:
    """One image of a skin, for one vehicle slot.

    Attributes:
        vehicle: The vehicle id.
        slot: The slot id; None until the loader fills in the vehicle's main slot.
        image: The source image.
        line: Line of the ``<target>`` element.
    """

    vehicle: str
    slot: str | None
    image: Path
    line: int


@dataclass
class Skin:
    """A paint job (``<skin>`` in project.xml).

    Attributes:
        id: Internal name (SCS token), part of every file name of the skin.
        name: Name shown in the game's shop.
        price: Price in the game's currency.
        unlock: Player level that unlocks it.
        icon: Shop icon source image; None = the first target's image.
        mode: "airbrush", "colormask" or "plain".
        colors: SII colour key -> linear RGB.
        raw_attrs: Extra ``(sii key, rendered value)`` pairs copied into the definition.
        targets: The images per vehicle slot.
        line: Line of the ``<skin>`` element.
    """

    id: str
    name: str
    price: int
    unlock: int
    icon: Path | None
    mode: str
    colors: dict[str, tuple[float, float, float]]
    raw_attrs: list[tuple[str, str]]
    targets: list[Target]
    line: int


@dataclass
class ModInfo:
    """The ``<mod>`` element: what goes into manifest.sii.

    Attributes:
        package: Package name (SCS token), names the mod's files.
        title: Display name.
        author: Author.
        version: Package version text.
        game: "ets2" or "ats".
        compatible: ``compatible_versions`` entries; empty by default (none written).
        categories: Mod manager categories.
        description: Description text file, or None for a generated one.
        icon: Mod manager icon source, or None for the first skin image.
    """

    package: str
    title: str
    author: str
    version: str
    game: str
    compatible: list[str]
    categories: list[str]
    description: Path | None
    icon: Path | None


@dataclass
class Project:
    """A loaded ``project.xml`` project, with every problem found as :attr:`issues`.

    Attributes:
        root: The project folder.
        file: The project.xml file.
        mod: Mod info, or None if ``<mod>`` is missing.
        output: The .scs/.zip file or folder to build to.
        compression: "store", "fast" or "small".
        icon_size: Shop icon size.
        texture_format: "DXT5" or "DXT1" for masks.
        vehicles_dirs: Folders the vehicle definitions came from.
        vehicles: Vehicle id -> definition.
        skins: The skins, in file order.
        issues: Every problem found while loading.
        watched: Files whose change should trigger a reload (watch mode).
    """

    root: Path
    file: Path
    mod: ModInfo | None = None
    output: Path | None = None
    compression: str = "fast"
    icon_size: tuple[int, int] = (256, 64)
    texture_format: str = "DXT5"
    vehicles_dirs: list[Path] = field(default_factory=list)
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    skins: list[Skin] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    watched: set[Path] = field(default_factory=set)

    @property
    def errors(self) -> list[Issue]:
        """The issues of level "error"."""
        return [i for i in self.issues if i.level == "error"]

    def rel(self, p: Path) -> str:
        """``p`` relative to the project root, '/'-separated, or as given if outside it."""
        try:
            return str(Path(p).resolve().relative_to(self.root.resolve())).replace("\\", "/")
        except ValueError:
            return str(p)


# --------------------------------------------------------------------------
# Value parsing
# --------------------------------------------------------------------------


def parse_color(value: str) -> tuple[float, float, float]:
    """Parse a colour for an SII colour key.

    Args:
        value: ``#rrggbb`` / ``#rgb`` (sRGB, converted to the game's linear values and rounded to
            4 decimals) or ``r,g,b`` floats 0-1 (used as they are).

    Raises:
        ValueError: The text is not a colour, or a value is outside 0-1 or not finite.
    """
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
            raise ValueError(f"bad hex colour {value!r}")
        r, g, b = (round(srgb_to_linear(int(h[i : i + 2], 16) / 255), 4) for i in (0, 2, 4))
        return r, g, b
    parts = [p for p in re.split(r"[,\s()]+", v) if p]
    if len(parts) != 3:
        raise ValueError(f"colour {value!r} must be #rrggbb or r,g,b")
    r, g, b = (float(p) for p in parts)
    # "nan" and "inf" parse as floats but would end up in the .sii; NaN also fails both bounds tests
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (r, g, b)):
        raise ValueError(f"colour {value!r}: r,g,b values must be between 0 and 1")
    return r, g, b


def _int(value: str | None, default: int, issues: list[Issue], file: str, line: int, name: str) -> int:
    """``value`` as an int, ``default`` when empty; a non-number adds an error and gives ``default``."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        issues.append(Issue("error", file, line, f"{name}={value!r} is not a whole number"))
        return default


def _path_attr(root_dir: Path, node: Node, key: str) -> Path | None:
    """The attribute ``key`` of ``node`` as a path relative to the project, or None if empty."""
    v = node.attrib.get(key, "").strip()
    return (root_dir / v) if v else None


# --------------------------------------------------------------------------
# Loading: vehicles
# --------------------------------------------------------------------------


def _read_xml(path: Path, project: Project) -> Node | None:
    """Read and parse ``path`` (and watch it); problems become issues and give None."""
    rel = project.rel(path)
    project.watched.add(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        project.issues.append(Issue("error", rel, 0, f"cannot read file: {e.strerror}"))
        return None
    try:
        return parse_xml(text)
    except ValueError as e:
        msg, line = e.args
        project.issues.append(Issue("error", rel, line, f"XML syntax error: {msg}"))
        return None


def _vehicle_template(n: Node, project: Project, rel: str) -> tuple[int, int] | None:
    """The ``<template width height>`` of a vehicle, or None when absent or not positive."""
    t = n.find("template")
    if t is None:
        return None
    w = _int(t.attrib.get("width"), 0, project.issues, rel, t.line, "width")
    h = _int(t.attrib.get("height"), 0, project.issues, rel, t.line, "height")
    return (w, h) if w > 0 and h > 0 else None


def _vehicle_slots(n: Node, project: Project, rel: str) -> dict[str, Slot] | None:
    """The slots of a vehicle, with an implicit main slot "default" when none is marked main.

    Returns:
        The slots, or None (after adding an error) when more than one slot is main.
    """
    slots: dict[str, Slot] = {}
    for s in n.find_all("slot"):
        sid = s.attrib.get("id", "").strip()
        if not TOKEN_RE.match(sid):
            project.issues.append(
                Issue("error", rel, s.line, f"slot id {sid!r} must be 1-12 chars [a-z0-9_]")
            )
            continue
        if sid in slots:
            project.issues.append(Issue("error", rel, s.line, f"duplicate slot {sid!r}"))
            continue
        accs = [a.text.strip() for a in s.find_all("acc") if a.text.strip()]
        main = s.attrib.get("main", "false").lower() == "true"
        if not main and not accs:
            project.issues.append(
                Issue("error", rel, s.line, f'slot {sid!r} needs main="true" or at least one <acc> entry')
            )
            continue
        slots[sid] = Slot(sid, main, accs)
    mains = [s for s in slots.values() if s.main]
    if not mains:
        if slots:
            project.issues.append(
                Issue(
                    "warning", rel, n.line, "no slot has main=\"true\"; adding implicit main slot 'default'"
                )
            )
        return {"default": Slot("default", True, []), **slots}
    if len(mains) > 1:
        project.issues.append(Issue("error", rel, n.line, 'only one slot may have main="true"'))
        return None
    return slots


def _load_vehicle(n: Node, project: Project, rel: str) -> None:
    """Check one ``<vehicle>`` element and add it to ``project.vehicles`` (a later one wins)."""
    vid = n.attrib.get("id", "").strip()
    if not vid or not all(TOKEN_RE.match(t) for t in vid.split(".")):
        project.issues.append(
            Issue(
                "error",
                rel,
                n.line,
                f"vehicle id {vid!r} must be dot-separated tokens of 1-12 chars [a-z0-9_] "
                "(e.g. scania.s_2016)",
            )
        )
        return
    kind = n.attrib.get("kind", "truck")
    if kind not in KINDS:
        project.issues.append(Issue("error", rel, n.line, f"kind={kind!r} must be one of {sorted(KINDS)}"))
        return
    game = n.attrib.get("game", "")
    if game and game not in GAMES:
        project.issues.append(Issue("warning", rel, n.line, f"game={game!r} is not ets2/ats"))
    template = _vehicle_template(n, project, rel)
    slots = _vehicle_slots(n, project, rel)
    if slots is None:
        return
    if vid in project.vehicles:
        other = project.vehicles[vid]
        project.issues.append(
            Issue(
                "warning",
                rel,
                n.line,
                f"vehicle {vid!r} also defined in {other.file}:{other.line}; this definition wins",
            )
        )
    project.vehicles[vid] = Vehicle(vid, kind, n.attrib.get("name", vid), game, template, slots, rel, n.line)


def load_vehicle_file(path: Path, project: Project) -> None:
    """Load the ``<vehicle>`` or ``<vehicles>`` file ``path`` into ``project.vehicles``."""
    root = _read_xml(path, project)
    if root is None:
        return
    rel = project.rel(path)
    nodes = [root] if root.tag == "vehicle" else root.find_all("vehicle") if root.tag == "vehicles" else []
    if not nodes:
        project.issues.append(Issue("error", rel, root.line, "expected <vehicle> or <vehicles> root element"))
    for n in nodes:
        _load_vehicle(n, project, rel)


# --------------------------------------------------------------------------
# Loading: project.xml sections
# --------------------------------------------------------------------------


def _load_mod(root: Node, project: Project, rel: str) -> None:
    """The ``<mod>`` element -> ``project.mod``; also checks the description and icon files."""
    m = root.find("mod")
    if m is None:
        project.issues.append(Issue("error", rel, root.line, "missing <mod> element"))
        return
    pkg = m.attrib.get("package", "").strip()
    if not TOKEN_RE.match(pkg):
        project.issues.append(
            Issue(
                "error",
                rel,
                m.line,
                f"package={pkg!r} must be 1-12 chars [a-z0-9_] (it names the mod's files)",
            )
        )
    game = m.attrib.get("game", "ets2")
    if game not in GAMES:
        project.issues.append(Issue("error", rel, m.line, f"game={game!r} must be ets2 or ats"))
    compat = [c.strip() for c in m.attrib.get("compatible", "").split(",") if c.strip()]
    cats = [c.strip() for c in m.attrib.get("categories", "paint_job").split(",") if c.strip()]
    project.mod = ModInfo(
        pkg,
        m.attrib.get("title", pkg),
        m.attrib.get("author", ""),
        m.attrib.get("version", "1.0"),
        game,
        compat,
        cats,
        _path_attr(project.root, m, "description"),
        _path_attr(project.root, m, "icon"),
    )
    for p in (project.mod.description, project.mod.icon):
        if p is not None:
            project.watched.add(p)
            if not p.is_file():
                project.issues.append(Issue("error", rel, m.line, f"file not found: {project.rel(p)}"))


def _load_build(root: Node, project: Project, rel: str) -> None:
    """The ``<build>`` element -> output path, compression, texture format and icon size."""
    b = root.find("build")
    out = b.attrib.get("output", "").strip() if b is not None else ""
    pkg_name = project.mod.package if project.mod else "mod"
    project.output = project.root / (out or f"build/{pkg_name}.scs")
    if b is None:
        return
    comp = b.attrib.get("compression", "fast")
    if comp not in ("store", "fast", "small"):
        project.issues.append(Issue("error", rel, b.line, "compression must be store, fast or small"))
        comp = "fast"
    project.compression = comp
    fmt = b.attrib.get("texture_format", "DXT5").upper()
    if fmt not in ("DXT5", "DXT1"):
        project.issues.append(Issue("error", rel, b.line, "texture_format must be DXT5 or DXT1"))
        fmt = "DXT5"
    project.texture_format = fmt
    isz = b.attrib.get("icon_size", "")
    if isz:
        try:
            w, h = (int(x) for x in isz.lower().split("x"))
            project.icon_size = (w, h)
        except ValueError:
            project.issues.append(Issue("error", rel, b.line, "icon_size must look like 256x64"))


def _load_vehicles(root: Node, project: Project, rel: str) -> None:
    """Load every ``*.xml`` of the ``<vehicles dir>`` folders (default: ``vehicles``)."""
    vdirs = [n.attrib.get("dir", "").strip() for n in root.find_all("vehicles")] or ["vehicles"]
    for d in vdirs:
        vdir = project.root / d
        project.vehicles_dirs.append(vdir)
        if not vdir.is_dir():
            project.issues.append(Issue("error", rel, 0, f"vehicles folder not found: {d}"))
            continue
        for f in sorted(vdir.glob("*.xml")):
            load_vehicle_file(f, project)


def _skin_colors(s: Node, project: Project, rel: str) -> dict[str, tuple[float, float, float]]:
    """The colour attributes of a ``<skin>``; warns about attributes it doesn't know."""
    colors = {}
    for key in COLOR_KEYS:
        if key in s.attrib:
            try:
                colors[key] = parse_color(s.attrib[key])
            except ValueError as e:
                project.issues.append(Issue("error", rel, s.line, f"{key}: {e}"))
    known = {"id", "name", "price", "unlock", "icon", "mode", *COLOR_KEYS}
    for k in s.attrib:
        if k not in known:
            project.issues.append(
                Issue(
                    "warning",
                    rel,
                    s.line,
                    f'unknown attribute {k!r} ignored (use <attr name="{k}" value="..."/> to pass raw SII)',
                )
            )
    return colors


def _skin_raw_attrs(s: Node, project: Project, rel: str) -> list[tuple[str, str]]:
    """The ``<attr name value>`` children of a ``<skin>``: raw SII lines for its definition."""
    raw = []
    for a in s.find_all("attr"):
        k, v = a.attrib.get("name", "").strip(), a.attrib.get("value", "").strip()
        if not _RAW_KEY_RE.fullmatch(k) or not v:
            project.issues.append(Issue("error", rel, a.line, '<attr> needs name="sii_key" and value="..."'))
            continue
        raw.append((k, v))
    return raw


def _skin_targets(s: Node, project: Project, rel: str) -> list[Target]:
    """The ``<target vehicle slot image>`` children of a ``<skin>`` whose image exists."""
    targets = []
    for t in s.find_all("target"):
        vid = t.attrib.get("vehicle", "").strip()
        img = _path_attr(project.root, t, "image")
        slot = t.attrib.get("slot", "").strip() or None
        if not vid or img is None:
            project.issues.append(Issue("error", rel, t.line, '<target> needs vehicle="..." and image="..."'))
            continue
        project.watched.add(img)
        if not img.is_file():
            project.issues.append(Issue("error", rel, t.line, f"image not found: {project.rel(img)}"))
            continue
        if img.suffix.lower() not in IMAGE_EXTS:
            project.issues.append(Issue("warning", rel, t.line, f"unusual image type {img.suffix}"))
        targets.append(Target(vid, slot, img, t.line))
    return targets


def _load_skin(s: Node, project: Project, rel: str, seen: set[str]) -> None:
    """Check one ``<skin>`` element and append it to ``project.skins``."""
    sid = s.attrib.get("id", "").strip()
    if not TOKEN_RE.match(sid):
        project.issues.append(
            Issue("error", rel, s.line, f"skin id {sid!r} must be 1-12 chars [a-z0-9_] (game limit)")
        )
        return
    if sid in seen:
        project.issues.append(Issue("error", rel, s.line, f"duplicate skin id {sid!r}"))
        return
    seen.add(sid)
    mode = s.attrib.get("mode", "airbrush")
    if mode not in MODES:
        project.issues.append(Issue("error", rel, s.line, f"mode={mode!r} must be one of {sorted(MODES)}"))
        mode = "airbrush"
    colors = _skin_colors(s, project, rel)
    raw = _skin_raw_attrs(s, project, rel)
    icon = _path_attr(project.root, s, "icon")
    if icon is not None:
        project.watched.add(icon)
        if not icon.is_file():
            project.issues.append(Issue("error", rel, s.line, f"icon not found: {project.rel(icon)}"))
            icon = None
    targets = _skin_targets(s, project, rel)
    if not targets:
        project.issues.append(
            Issue("warning", rel, s.line, f"skin {sid!r} has no targets and will be skipped")
        )
    project.skins.append(
        Skin(
            sid,
            s.attrib.get("name", sid),
            _int(s.attrib.get("price"), 1, project.issues, rel, s.line, "price"),
            _int(s.attrib.get("unlock"), 0, project.issues, rel, s.line, "unlock"),
            icon,
            mode,
            colors,
            raw,
            targets,
            s.line,
        )
    )


def _cross_check_skin(skin: Skin, project: Project, rel: str) -> None:
    """Check a skin's targets against the vehicles, and fill in each target's default slot."""
    per_vehicle: dict[str, set[str]] = {}
    for t in skin.targets:
        v = project.vehicles.get(t.vehicle)
        if v is None:
            project.issues.append(
                Issue(
                    "error",
                    rel,
                    t.line,
                    f"unknown vehicle {t.vehicle!r} (define it in a vehicles/*.xml file)",
                )
            )
            continue
        slot = t.slot or v.main_slot.id
        if slot not in v.slots:
            project.issues.append(
                Issue(
                    "error",
                    rel,
                    t.line,
                    f"vehicle {t.vehicle!r} has no slot {slot!r} (has: {', '.join(v.slots)})",
                )
            )
            continue
        if slot in per_vehicle.setdefault(t.vehicle, set()):
            project.issues.append(Issue("error", rel, t.line, f"slot {slot!r} of {t.vehicle!r} given twice"))
        per_vehicle[t.vehicle].add(slot)
        t.slot = slot
        if project.mod and v.game and v.game != project.mod.game:
            project.issues.append(
                Issue(
                    "warning",
                    rel,
                    t.line,
                    f"vehicle {t.vehicle!r} is for {v.game}, mod is for {project.mod.game}",
                )
            )
    for vid, slots in per_vehicle.items():
        veh = project.vehicles.get(vid)
        if veh and veh.main_slot.id not in slots:
            project.issues.append(
                Issue(
                    "error",
                    rel,
                    skin.line,
                    f"skin {skin.id!r} on {vid!r} needs an image for the main slot {veh.main_slot.id!r}",
                )
            )


def load_project(project_dir: str | Path) -> Project:
    """Load a ``project.xml`` project and everything it refers to.

    Args:
        project_dir: The project folder, or the project.xml file itself.

    Returns:
        The project. It is always returned; check :attr:`Project.errors` before building.
    """
    root_dir = Path(project_dir)
    pfile = root_dir / "project.xml" if root_dir.is_dir() else root_dir
    project = Project(root=pfile.parent, file=pfile)
    root = _read_xml(pfile, project)
    if root is None:
        return project
    rel = project.rel(pfile)
    if root.tag != "project":
        project.issues.append(Issue("error", rel, root.line, "root element must be <project>"))
        return project
    _load_mod(root, project, rel)
    _load_build(root, project, rel)
    _load_vehicles(root, project, rel)
    seen: set[str] = set()
    for s in root.find_all("skin"):
        _load_skin(s, project, rel, seen)
    for skin in project.skins:
        _cross_check_skin(skin, project, rel)
    return project
