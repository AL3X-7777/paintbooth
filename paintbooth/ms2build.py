"""Export a Mods Studio 2 project (.ms2/.pb) to a game mod.

:func:`validate` checks a project and returns :class:`Problem` entries for the GUI;
:func:`plan_ms2` turns a valid project into a list of :class:`~paintbooth.builder.OutFile`
that :meth:`paintbooth.builder.Builder.build_files` encodes and packs. Template items
(lightboxes, decals, ...) are delegated to :mod:`paintbooth.tplbuild`.

Output layout for an Advanced Truck Skin item with internal name N on truck T
(one paint job per cab image, so each cab layout gets its own texture)::

  def/vehicle/truck/T/paint_job/N_<i>.sii            accessory_paint_job_data N_<i>.T.paint_job
                                                     with suitable_for[] = the cabins of image i
  def/vehicle/truck/T/paint_job/accessory/N_<i>.sii  simple_paint_job_data .ovr<k> per accessory image
  vehicle/truck/upgrade/paintjob/T/N/N_<i>.dds/.tobj
  vehicle/truck/upgrade/paintjob/T/N/accessories/acc_<k>.dds/.tobj
  material/ui/accessory/N.dds/.tobj/.mat             shop icon
  manifest.sii, mod_icon.jpg, mod_description.txt

Trailers (vehicle type "Trailers") get the owned-trailer layout instead: one paint job N for
the whole trailer, whose skin images are applied to the bodies they are "suitable for" through
accessory overrides::

  def/vehicle/trailer_owned/T/paint_job/N.sii             accessory_paint_job_data N.T.paint_job
  def/vehicle/trailer_owned/T/paint_job/accessory/N.sii   .ovr per skin image (acc_list = bodies)
                                                          and per accessory image
  vehicle/trailer_owned/upgrade/paintjob/T/N/...          textures

``manifest.sii`` only gets ``compatible_versions[]`` lines the user typed into the project;
new projects leave that list empty.

Performance: the GUI calls :func:`resolve` and :func:`validate` on the Tk thread after every
edit, so the expensive parts are cached: the recursive file-name index of a project folder
(:func:`_tree_lookup`, refreshed every few seconds) and image sizes (by path, mtime and size).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from PIL import Image

from .builder import MOD_ICON_SIZE, OutFile, TextureJob
from .formats import dds_size, is_dds, sii_file, sii_float3, sii_str, tobj_bytes, ui_mat
from .ms2 import SKIN_NAME_LIMIT, AdvancedTruckSkin, Item, Ms2Project, SuitableImage, TemplateItem, Vars
from .names import TokenIssue, token_issue
from .util import is_pot

if TYPE_CHECKING:
    from . import vehlib

log = logging.getLogger(__name__)

ICON_SIZE = (256, 64)
VERSION_RE = re.compile(r"(?:\*|\d+)\.(?:\*|\d+)(?:\.(?:\*|\d+))?")

# (sii attribute, MS2 variable) of the metallic (flipflake) floats, written with two decimals
_METALLIC_FLOATS = (
    ("flip_strength", "FlipStrenght"),  # sic: MS2's variable name
    ("flake_uvscale", "FlakeUVScale"),
    ("flake_density", "FlakeDensity"),
    ("flake_shininess", "FlakeShininess"),
    ("flake_clearcoat_rolloff", "FlakeClearcoat"),
)
_MASK_COLORS = (("r", "RedColor"), ("g", "GreenColor"), ("b", "BlueColor"))

# A sii attribute list: (key, already formatted value)
Attrs = list[tuple[str, str]]
# An override unit: (class, unit name, attributes)
Unit = tuple[str, str, Attrs]


@dataclass
class Problem:
    """One validation result, shown next to the field it concerns.

    Attributes:
        level: ``"error"`` (blocks export) or ``"warning"``.
        item: Index of the item in the project, or -1 for the mod manifest.
        field: The variable name, e.g. ``"InternalName"`` or ``"CabVariantSkin[0]"``
            for an image entry; ``""`` for the item as a whole.
        message: Text for the user.
    """

    level: str  # "error" | "warning"
    item: int  # -1 = mod manifest, otherwise item index
    field: str  # variable name, e.g. "InternalName", "CabVariantSkin[0]"
    message: str


# --------------------------------------------------------------------------
# Finding image files
# --------------------------------------------------------------------------

_INDEX_TTL = 5.0  # seconds a folder index is trusted for misses
_INDEX_MAX_ENTRIES = 500_000  # stop indexing huge trees (e.g. a project saved in the drive root)
_INDEX_MAX_FOLDERS = 8


@dataclass
class _FolderIndex:
    """First path found for each file name below a folder, in ``Path.rglob`` order."""

    built: float
    names: dict[str, Path]


_indexes: OrderedDict[str, _FolderIndex] = OrderedDict()
_index_lock = threading.Lock()


def _name_key(name: str) -> str:
    """Return the index key for a file name (case-insensitive on Windows, like ``rglob`` there)."""
    return os.path.normcase(name)


def _build_index(base: Path) -> _FolderIndex:
    """Walk ``base`` once and record the first entry for every name.

    The walk uses ``rglob("*")``, which visits entries in the same order as the
    ``rglob(name)`` this replaces, so the same file wins when a name occurs twice.
    """
    names: dict[str, Path] = {}
    try:
        for n, p in enumerate(base.rglob("*")):
            if n >= _INDEX_MAX_ENTRIES:
                log.warning("stopped indexing %s after %d entries", base, n)
                break
            names.setdefault(_name_key(p.name), p)
    except OSError as e:  # unreadable folder: index what was found so far
        log.debug("indexing %s: %s", base, e)
    return _FolderIndex(time.monotonic(), names)


def _tree_lookup(base: Path, name: str) -> Path | None:
    """Return a file called ``name`` anywhere below ``base``, or None.

    Uses a cached index of ``base``. A miss is trusted for :data:`_INDEX_TTL` seconds,
    so typing a path character by character does not walk the tree each time; a hit
    whose file has disappeared rebuilds the index at once.
    """
    key = str(base)
    with _index_lock:
        idx = _indexes.get(key)
        if idx is not None:
            _indexes.move_to_end(key)
    stale = idx is None or time.monotonic() - idx.built > _INDEX_TTL
    hit = None if idx is None else idx.names.get(_name_key(name))
    if hit is not None and not hit.is_file():
        stale = True
    if stale:
        idx = _build_index(base)
        with _index_lock:
            _indexes[key] = idx
            _indexes.move_to_end(key)
            while len(_indexes) > _INDEX_MAX_FOLDERS:
                _indexes.popitem(last=False)
        hit = idx.names.get(_name_key(name))
    return hit if hit is not None and hit.is_file() else None


def clear_resolve_cache() -> None:
    """Forget every folder index and image size (e.g. after the user picked new files)."""
    with _index_lock:
        _indexes.clear()
    _image_sizes.clear()


def resolve(path_text: str, base: Path | None) -> Path | None:
    """Find an image the project refers to.

    Tries the path as written, then the same path in the renamed Paintbooth folder,
    then relative to the project folder, and finally the file name anywhere in the
    project folder (so projects survive being moved to another PC or folder).

    Args:
        path_text: The path stored in the project (often an absolute Windows path).
        base: The folder of the project file, or None for an unsaved project.

    Returns:
        The file, or None if it cannot be found.
    """
    if not path_text:
        return None
    p = Path(path_text)
    if p.is_file():
        return p
    from .appdirs import moved_path  # files that were in an earlier Documents folder

    moved = moved_path(path_text)
    if moved != path_text and Path(moved).is_file():
        return Path(moved)
    if base is None:
        return None
    win = PureWindowsPath(path_text)
    # "C:\Users\x\skins\a.png" -> <base>/Users/x/skins/a.png: the same tree copied elsewhere
    for cand in (base / path_text, base / Path(*win.parts[1:]) if win.drive else None):
        if cand is not None and cand.is_file():
            return cand
    name = win.name
    if (base / name).is_file():
        return base / name
    return _tree_lookup(base, name) if name else None


_IMAGE_SIZE_CACHE_MAX = 1024
_image_sizes: OrderedDict[tuple[str, int, int], tuple[int, int] | None] = OrderedDict()


def _read_image_size(p: Path) -> tuple[int, int] | None:
    """Return (width, height) of an image file without decoding it, or None if it cannot be read."""
    try:
        if p.suffix.lower() == ".dds":
            with p.open("rb") as fh:
                head = fh.read(128)
            if is_dds(head):
                return dds_size(head)
        with Image.open(p) as im:
            return im.size
    except (OSError, ValueError, SyntaxError, struct.error, Image.DecompressionBombError) as e:
        # PIL raises SyntaxError/ValueError for some damaged files; all mean "cannot read"
        log.debug("cannot read image size of %s: %s", p, e)
        return None


def image_size(p: Path) -> tuple[int, int] | None:
    """Return (width, height) of image ``p`` (PNG, JPG, DDS, ...), or None if it cannot be read.

    Results are cached by path, modification time and size, so repeated validation
    does not reopen unchanged images.
    """
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key in _image_sizes:
        _image_sizes.move_to_end(key)
        return _image_sizes[key]
    size = _read_image_size(p)
    _image_sizes[key] = size
    while len(_image_sizes) > _IMAGE_SIZE_CACHE_MAX:
        _image_sizes.popitem(last=False)
    return size


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _is_finite(text: str) -> bool:
    """Return True if ``text`` (empty counts as 0, like the exporter reads it) is a finite number."""
    try:
        return math.isfinite(float(text or 0))
    except ValueError:
        return False


def _validate_manifest(project: Ms2Project, base: Path | None) -> list[Problem]:
    """Check the mod manifest: name, internal name, icon and compatible versions."""
    probs: list[Problem] = []
    m = project.manifest
    if not m.vars.value("display_name").strip():
        probs.append(Problem("error", -1, "display_name", "Mod name is empty"))
    gname = m.vars.value("GlobalInternalName")
    # an empty package name is allowed (the exporter uses "package_name"); no length limit here,
    # to keep accepting the projects Mods Studio 2 accepts
    if gname and token_issue(gname, max_len=None) is not None:
        probs.append(
            Problem("error", -1, "GlobalInternalName", "Mod internal name may only use a-z, 0-9 and _")
        )
    icon = m.vars.value("icon")
    if icon and resolve(icon, base) is None:
        probs.append(
            Problem(
                "warning",
                -1,
                "icon",
                f"Mod icon not found ({PureWindowsPath(icon).name}); the default icon will be used",
            )
        )
    for v in m.vars.strings("compatible_versions"):
        if not VERSION_RE.fullmatch(v.strip()):
            probs.append(Problem("error", -1, "compatible_versions", f"'{v}' is not a version like 1.50.* "))
    return probs


_SKIN_NAME_MESSAGES = {
    TokenIssue.EMPTY: "Skin internal name is empty",
    TokenIssue.CHARS: "Internal name may only use a-z, 0-9 and _",
    # each cab image adds "_<i>", so 10 + 2 stays within the game's 12-character token limit
    TokenIssue.LENGTH: "Internal name is longer than 10 characters (game limit is 12 incl. the _0 suffix)",
}


def _validate_skin_name(it: AdvancedTruckSkin, idx: int, seen: dict[str, int]) -> list[Problem]:
    """Check a skin's internal name; record it in ``seen`` (name -> item index) when it is valid."""
    name = it.get("InternalName")
    issue = token_issue(name, SKIN_NAME_LIMIT)
    msg = _SKIN_NAME_MESSAGES[issue] if issue is not None else None
    if msg is None and name in seen:
        msg = f"Internal name '{name}' is also used by item {seen[name] + 1}"
    if msg is not None:
        return [Problem("error", idx, "InternalName", msg)]
    seen[name] = idx
    return []


def _metal_and_mask(v: Vars) -> tuple[bool, bool]:
    """Return (metallic, mask) as exported: the game cannot combine them, so the mask wins."""
    mask_on = v.bool("EnableMask")
    return v.bool("EnableMetallic") and not mask_on, mask_on


def _validate_numbers(it: AdvancedTruckSkin, idx: int) -> list[Problem]:
    """Check price, unlock level and every colour and float the export will write."""
    probs: list[Problem] = []
    for f in ("SkinPrice", "SkinUnlock"):
        try:
            int(it.get(f, "0") or 0)
        except ValueError:
            probs.append(Problem("error", idx, f, "Must be a whole number"))
    metal_on, mask_on = _metal_and_mask(it.vars)
    colors = ["BaseColor"]
    floats: list[str] = []
    if metal_on:
        colors += ["FlipColor", "FlakeColor"]
        floats += [var for _key, var in _METALLIC_FLOATS]
    if mask_on:
        colors += [var for _ch, var in _MASK_COLORS]
    for name in colors:
        var = it.vars.get(name)
        if var is not None and not all(_is_finite(var.get_text(c, "0")) for c in "RGB"):
            probs.append(Problem("error", idx, name, "Colour values must be numbers"))
    for name in floats:
        if not _is_finite(it.vars.value(name)):
            probs.append(Problem("error", idx, name, "Must be a number"))
    return probs


def _validate_entry(
    e: SuitableImage, idx: int, fld: str, what: str, base: Path | None, any_size: bool = False
) -> list[Problem]:
    """Check one skin or accessory image entry (``what`` is e.g. "Skin image 1").

    ``any_size`` skips the power-of-two rule, for coach blueprints (they are baked into a
    texture of the right size, so the image itself can be any size).
    """
    probs: list[Problem] = []
    p = resolve(e.image, base)
    if not e.image:
        probs.append(Problem("error", idx, fld, f"{what}: no image selected"))
    elif p is None:
        probs.append(Problem("error", idx, fld, f"{what}: file not found ({PureWindowsPath(e.image).name})"))
    else:
        size = image_size(p)
        if size is None:
            probs.append(Problem("error", idx, fld, f"{what}: cannot read {p.name}"))
        elif not any_size and not (is_pot(size[0]) and is_pot(size[1])):
            probs.append(
                Problem(
                    "error",
                    idx,
                    fld,
                    f"{what}: {size[0]}x{size[1]} is not a power of two (e.g. 4096x4096)",
                )
            )
    return probs


def _validate_images(it: AdvancedTruckSkin, idx: int, base: Path | None) -> list[Problem]:
    """Check the skin and accessory image entries: file present, readable, power-of-two, ticked."""
    probs: list[Problem] = []
    if not it.entries("CabVariantSkin"):
        probs.append(Problem("error", idx, "CabVariantSkin", "Add at least one skin image"))
    veh = it.vehicle()
    blueprint = veh is not None and veh.is_bus and not it.vars.bool(UV_LAYOUT)
    for arr, label in (("CabVariantSkin", "Skin"), ("AccessorySkins", "Accessory")):
        for k, e in enumerate(it.entries(arr)):
            fld = f"{arr}[{k}]"
            what = f"{label} image {k + 1}"
            probs += _validate_entry(e, idx, fld, what, base, any_size=blueprint and arr == "CabVariantSkin")
            if not e.suitable:
                kind = "cabin" if arr == "CabVariantSkin" else "accessory"
                probs.append(Problem("error", idx, fld, f"{what}: tick at least one {kind}"))
    return probs


def _cab_masks(it: AdvancedTruckSkin) -> dict[str, str | None]:
    """Map each cabin of the skin's vehicle to its paint job mask region (buses; else None)."""
    veh = it.vehicle()
    return {x.id: x.mask for x in veh.variants} if veh is not None else {}


def _validate_bus(it: AdvancedTruckSkin, idx: int, base: Path | None) -> list[Problem]:
    """Check the coach-only rules of a skin.

    Images should be 2:1; blueprints need the game folder (they are baked from the game's
    models); images drawn on the game's UV layout must not mix bodies of different layouts.
    """
    veh = it.vehicle()
    if veh is None or not veh.is_bus:
        return []
    probs: list[Problem] = []
    uv_layout = it.vars.bool(UV_LAYOUT)
    masks = _cab_masks(it)
    for k, e in enumerate(it.entries("CabVariantSkin")):
        fld = f"CabVariantSkin[{k}]"
        bodies = body_ids(e.suitable)
        lone = [c[: -len(FILMS)] for c in e.suitable if c.endswith(FILMS) and c[: -len(FILMS)] not in bodies]
        if lone:
            probs.append(
                Problem(
                    "error",
                    idx,
                    fld,
                    f"Skin image {k + 1}: windows and trim are painted together with their body; "
                    "tick the body too",
                )
            )
        if uv_layout and len(bodies) < len(e.suitable):
            probs.append(
                Problem(
                    "error",
                    idx,
                    fld,
                    "Painting windows and trim needs images drawn on the blueprint template",
                )
            )
        if uv_layout and len({masks.get(c) for c in bodies}) > 1:
            probs.append(
                Problem(
                    "error",
                    idx,
                    fld,
                    f"Skin image {k + 1}: the ticked bodies use different template layouts; "
                    "use one image per body",
                )
            )
        src = resolve(e.image, base)
        size = image_size(src) if src else None
        if size and size[0] != 2 * size[1]:
            probs.append(
                Problem(
                    "warning",
                    idx,
                    fld,
                    f"Skin image {k + 1} is {size[0]}x{size[1]}; coach templates are 2:1 (4096x2048)",
                )
            )
    if not uv_layout and not _game_dir():
        probs.append(
            Problem(
                "error",
                idx,
                "Truck",
                "Coach skins need your ETS2 game folder: Vehicles > Scan game folder once",
            )
        )
    return probs


def _game_dir() -> Path | None:
    """The ETS2 game folder from the settings (set by Vehicles > Scan game folder), or None."""
    from .tplbuild import _settings

    d = (_settings().get("game_dirs") or {}).get("ets2")
    return Path(d) if d and Path(d).is_dir() else None


def _validate_skin(it: AdvancedTruckSkin, idx: int, seen: dict[str, int], base: Path | None) -> list[Problem]:
    """Check one Advanced Truck Skin item."""
    probs = _validate_skin_name(it, idx, seen)
    if not it.get("SkinName").strip():
        probs.append(Problem("error", idx, "SkinName", "Skin name is empty"))
    probs += _validate_numbers(it, idx)
    if not it.truck_id:
        probs.append(Problem("error", idx, "Truck", "Select a truck or trailer"))
    probs += _validate_images(it, idx, base)
    probs += _validate_bus(it, idx, base)
    icon = it.get("SkinIcon")
    if icon and resolve(icon, base) is None:
        probs.append(
            Problem(
                "warning",
                idx,
                "SkinIcon",
                f"Icon not found ({PureWindowsPath(icon).name}); the default icon will be used",
            )
        )
    if it.vars.bool("EnableMask") and it.vars.bool("EnableMetallic"):
        probs.append(
            Problem(
                "warning",
                idx,
                "EnableMask",
                "The game can't combine mask colours with metallic (flipflake); metallic will be ignored",
            )
        )
    return probs


def _unsupported(it: Item, idx: int) -> Problem:
    """Return the warning for an item type Paintbooth keeps but cannot export."""
    return Problem(
        "warning",
        idx,
        "",
        f"'{it.template_name}' items are not supported yet and are skipped on export "
        "(kept in the project file)",
    )


def validate(project: Ms2Project) -> list[Problem]:
    """Check a project before export.

    Errors block :func:`plan_ms2`; warnings are shown but the mod can still be built.
    Internal names are checked for duplicates across skins and template items.

    Returns:
        Every problem found, manifest first, then items in project order.
    """
    base = project.path.parent if project.path else None
    probs = _validate_manifest(project, base)
    seen: dict[str, int] = {}
    for idx, it in enumerate(project.items):
        if isinstance(it, TemplateItem):
            from .tplbuild import validate_item

            probs += validate_item(project, idx, it, seen, Problem)
        elif isinstance(it, AdvancedTruckSkin):
            probs += _validate_skin(it, idx, seen, base)
        else:
            probs.append(_unsupported(it, idx))
    return probs


# --------------------------------------------------------------------------
# Names and versions
# --------------------------------------------------------------------------


def _f2(x: str) -> str:
    """Format an MS2 float for sii with two decimals (empty counts as 0)."""
    return f"{float(x or 0):.2f}"


def creator_suffix(name: str, creator: str) -> str:
    """'My mod' + 'AL3X' -> 'My mod by AL3X' (unchanged if it already ends that way)."""
    creator = creator.strip()
    if not creator:
        return name
    tail = f"by {creator}".lower()
    return name if name.strip().lower().endswith(tail) else f"{name.rstrip()} by {creator}"


def version_suffix(name: str, version: str) -> str:
    """'My mod by AL3X' + '1.02' -> 'My mod by AL3X V1.02' (version always at the very end).

    A version that already starts with v/V is used as written; nothing is added twice.
    """
    version = (version or "").strip()
    if not version:
        return name
    tag = version if version[:1] in "vV" else f"V{version}"
    base = name.rstrip()
    if base.lower().endswith(tag.lower()):
        return base
    return f"{base} {tag}" if base else tag


def export_name(name: str, settings: Mapping[str, object], version: str | None) -> str:
    """Return the mod name used in manifest.sii: optional 'by <creator>', then optional version.

    Args:
        name: The mod name from the project.
        settings: The user's settings (``append_creator``, ``creator_name``, ``append_version``).
        version: The version being exported, if any.
    """
    creator = settings.get("creator_name")
    if settings.get("append_creator") and creator:
        name = creator_suffix(name, str(creator))
    if settings.get("append_version") and version:
        name = version_suffix(name, version)
    return name


def next_version(current: str, last_exported: str | None) -> str:
    """Return the next automatic version number: 1.00, 1.01, ...

    Args:
        current: The version in the project.
        last_exported: The version used by the previous export, or None. The number is
            only increased when ``current`` was already exported.

    Returns:
        ``current`` itself if it was not exported yet; otherwise the next number. A version that is
        not in the ``N.NN`` form keeps its form and gets its last number raised (``2.10.1`` gives
        ``2.10.2``). An empty version starts at ``1.00``.
    """
    text = (current or "").strip()
    exported = last_exported is not None and text == last_exported
    m = re.fullmatch(r"v?(\d+)\.(\d{2})", text)
    if m:  # the usual form: 1.99 is followed by 2.00
        if not exported:
            return f"{int(m.group(1))}.{m.group(2)}"
        n = int(m.group(1)) * 100 + int(m.group(2)) + 1
        return f"{n // 100}.{n % 100:02d}"
    last = re.search(r"\d+(?!.*\d)", text)
    if last is None:  # empty, or no number at all
        return "1.00"
    if not exported:  # the user's own scheme (1.5, 2.10.1): keep it, never go back to 1.00
        return text
    bumped = str(int(last.group()) + 1).zfill(len(last.group()))  # raise the last number, keep its width
    return text[: last.start()] + bumped + text[last.end() :]


# --------------------------------------------------------------------------
# Export plan
# --------------------------------------------------------------------------


#: Bus skins: the option to paint windows and gloss-black trim, and its image (see paintbooth.coach).
PAINT_WINDOWS = "PaintWindows"
#: Coach skins: ticking ``<cabin id>`` + this in an image's list paints that body's windows and trim.
FILMS = "+films"


def body_ids(suitable: list[str]) -> list[str]:
    """The cabin/body ids of a coach image's ticked list, without the windows-and-trim ids."""
    return [c for c in suitable if not c.endswith(FILMS)]


#: Bus skins: the images are drawn on the game's UV layout, not on the blueprint (advanced).
UV_LAYOUT = "UvLayout"


@functools.lru_cache(maxsize=4)
def _builtin_icons(home: str) -> tuple[Path, Path]:
    """Render the default icons into the Paintbooth folder ``home`` once per process."""
    from .appdirs import default_icons

    return default_icons(None)


def _defaults(given: tuple[Path, Path] | None) -> tuple[Path, Path]:
    """Return the icons to fall back to: ``given``, else Paintbooth's (skin 256x64 PNG, mod 276x162 JPG)."""
    if given:
        return given
    from .appdirs import home

    icons = _builtin_icons(str(home()))
    if not all(p.is_file() for p in icons):  # deleted since: render again
        _builtin_icons.cache_clear()
        icons = _builtin_icons(str(home()))
    return icons


def _icon_files(
    it: AdvancedTruckSkin, n: str, base: Path | None, icons: tuple[Path, Path] | None
) -> list[OutFile]:
    """Return the shop icon of skin ``n``: texture (with the optional overlay), tobj and material."""
    icon_src = resolve(it.get("SkinIcon"), base) or _defaults(icons)[0]
    ov_var = it.vars.get("SkinIcon")
    ov_text = ""
    cv = ov_var.path("ChildVariables") if ov_var is not None else None
    for c in cv.children if cv is not None else []:
        if c.get_text("Name") == "OverlaySource":
            ov_text = c.get_text("Value")
    overlay = resolve(ov_text, base) if ov_text else None
    return [
        OutFile(
            f"material/ui/accessory/{n}.dds",
            job=TextureJob(icon_src, "icon", ICON_SIZE, "BGRA_SRGB", overlay),
        ),
        OutFile(
            f"material/ui/accessory/{n}.tobj", tobj_bytes(f"/material/ui/accessory/{n}.dds", profile="ui")
        ),
        OutFile(f"material/ui/accessory/{n}.mat", ui_mat(f"{n}.tobj").encode()),
    ]


def _locked(v: Vars, name: str, default: bool = False) -> str:
    """Return a ``*_locked`` attribute value from a BooleanVariable."""
    return "true" if v.bool(name, default) else "false"


def _paint_attrs(it: AdvancedTruckSkin, n: str) -> Attrs:
    """Return the attributes every paint job unit of skin ``n`` shares (name, price, colours, ...)."""
    v = it.vars
    metal_on, mask_on = _metal_and_mask(v)
    attrs: Attrs = [
        ("name", sii_str(it.get("SkinName"))),
        ("price", str(int(it.get("SkinPrice", "1") or 1))),
        ("unlock", str(int(it.get("SkinUnlock", "0") or 0))),
        ("icon", sii_str(n)),
        ("airbrush", "false" if mask_on else "true"),
        ("base_color", sii_float3(v.color("BaseColor"))),
        ("base_color_locked", _locked(v, "BaseColorLocked")),
        ("alternate_uvset", _locked(v, "AlternateUvSet")),
    ]
    if metal_on:
        attrs += [
            ("flipflake", "true"),
            ("flip_color", sii_float3(v.color("FlipColor"))),
            ("flip_strength", _f2(v.value("FlipStrenght"))),
            ("flip_color_locked", _locked(v, "FlipColorLocked")),
            ("flake_color", sii_float3(v.color("FlakeColor"))),
        ]
        attrs += [(key, _f2(v.value(var))) for key, var in _METALLIC_FLOATS[1:]]
        attrs += [
            ("flake_color_locked", _locked(v, "FlakeColorLocked")),
            ("flake_noise", sii_str("/material/custom/flake_noise.tobj")),
        ]
    if mask_on:
        for ch, var in _MASK_COLORS:
            attrs.append((f"mask_{ch}_color", sii_float3(v.color(var))))
            attrs.append((f"mask_{ch}_locked", _locked(v, f"{var}Locked", True)))
    return attrs


def _texture_files(path: str, src: Path | None) -> list[OutFile]:
    """Return a DXT5 skin texture at ``path`` (``.dds``) and its ``.tobj``."""
    stem = path[: -len(".dds")]
    # validate() guarantees every image resolved, so src is never None here
    return [
        OutFile(path, job=TextureJob(src, "mask", None, "DXT5")),  # type: ignore[arg-type]
        OutFile(f"{stem}.tobj", tobj_bytes(f"/{path}", profile="skin")),
    ]


def _plan_skin(it: AdvancedTruckSkin, base: Path | None, icons: tuple[Path, Path] | None) -> list[OutFile]:
    """Return every file of one Advanced Truck Skin item (see the module docstring for the layout)."""
    t, n = it.truck_id, it.get("InternalName")
    cabs = [(e, resolve(e.image, base)) for e in it.entries("CabVariantSkin")]
    accs = [(e, resolve(e.image, base)) for e in it.entries("AccessorySkins")]
    veh = it.vehicle()
    trailer = veh is not None and veh.is_trailer
    vt = "trailer_owned" if trailer else "bus" if veh is not None and veh.is_bus else "truck"
    pj_root = f"vehicle/{vt}/upgrade/paintjob/{t}/{n}"
    masks = _cab_masks(it)
    def_dir = f"def/vehicle/{vt}/{t}/paint_job"

    files = _icon_files(it, n, base, icons)
    attrs = _paint_attrs(it, n)

    # accessory textures (shared by every cab paint job)
    overrides: list[Unit] = []
    for k, (acc, src) in enumerate(accs):
        files += _texture_files(f"{pj_root}/accessories/acc_{k}.dds", src)
        kv = [("paint_job_mask", sii_str(f"/{pj_root}/accessories/acc_{k}.tobj"))]
        kv += [("acc_list[]", sii_str(a)) for a in acc.suitable]
        overrides.append(("simple_paint_job_data", f".ovr{k}", kv))

    if veh is not None and veh.is_bus and not it.vars.bool(UV_LAYOUT):
        return files + _plan_bus_blueprint(it, veh, cabs, attrs, def_dir, pj_root)

    # skin textures; a bus cabin's mask is named after its region of the paint UV space
    tex: list[str] = []
    for i, (cab, src) in enumerate(cabs):
        region = next((masks[c] for c in body_ids(cab.suitable) if masks.get(c)), None)
        stem = f"{pj_root}/{n}_{i}/{region}" if region else f"{pj_root}/{n}_{i}"
        tex.append(f"/{stem}.tobj")
        files += _texture_files(f"{stem}.dds", src)

    if trailer:
        # one paint job for the trailer; each skin image paints its bodies via an override
        body_ovr = []
        for i, (cab, _src) in enumerate(cabs):
            kv = [("paint_job_mask", sii_str(f"/{pj_root}/{n}_{i}.tobj"))]
            kv += [("acc_list[]", sii_str(c)) for c in body_ids(cab.suitable)]
            body_ovr.append(kv)
        all_ovr = [
            ("simple_paint_job_data", f".ovr{k}", kv)
            for k, kv in enumerate(body_ovr + [o[2] for o in overrides])
        ]
        unit_attrs = [*attrs, ("paint_job_mask", sii_str(f"/{pj_root}/{n}_0.tobj"))]
        files.append(
            OutFile(
                f"{def_dir}/{n}.sii",
                sii_file([("accessory_paint_job_data", f"{n}.{t}.paint_job", unit_attrs)]).encode(),
            )
        )
        files.append(OutFile(f"{def_dir}/accessory/{n}.sii", sii_file(all_ovr).encode()))
        return files

    # trucks: one paint job per cab image
    for i, (cab, _src) in enumerate(cabs):
        unit_attrs = list(attrs)
        unit_attrs += [("suitable_for[]", sii_str(c)) for c in body_ids(cab.suitable)]
        unit_attrs.append(("paint_job_mask", sii_str(tex[i])))
        unit = ("accessory_paint_job_data", f"{n}_{i}.{t}.paint_job", unit_attrs)
        files.append(OutFile(f"{def_dir}/{n}_{i}.sii", sii_file([unit]).encode()))
        if overrides:
            files.append(OutFile(f"{def_dir}/accessory/{n}_{i}.sii", sii_file(overrides).encode()))
    return files


def _plan_bus_blueprint(
    it: AdvancedTruckSkin,
    veh: vehlib.Vehicle,
    cabs: list[tuple[SuitableImage, Path | None]],
    attrs: Attrs,
    def_dir: str,
    pj_root: str,
) -> list[OutFile]:
    """Coach skins painted on the blueprint (see :mod:`paintbooth.coach`).

    Each (image, ticked body) pair becomes its own paint job ``N_j``: the blueprint is baked into
    that body's paint UV layout, because bodies of different length map the same image
    differently. Where ``<body>+films`` is ticked too, the paint job brings window and trim films
    showing the image.

    Raises:
        RuntimeError: The game folder or a body's model can't be read.
    """
    from . import coach
    from .scanner import game_reader

    t, n = it.truck_id, it.get("InternalName")
    game = _game_dir()
    if game is None:  # validate() reports it; plan_ms2 stops on errors first
        return []
    bodies = {x.id: x for x in veh.variants}
    files: list[OutFile] = []
    j = 0
    try:
        read = game_reader(game)
        for e, src in cabs:
            for cab_id in body_ids(e.suitable):
                x = bodies.get(cab_id)
                region = coach.Region.parse(x.mask or "") if x else None
                if x is None or src is None or region is None or not x.model:
                    continue
                body = coach.load_body(read, t, cab_id, x.model)
                stem = f"{pj_root}/{n}_{j}/{region.name}"
                files += _texture_files(f"{stem}.dds", _baked(body, region, src, x.model))
                unit_attrs = [
                    *attrs,
                    ("suitable_for[]", sii_str(cab_id)),
                    ("paint_job_mask", sii_str(f"/{stem}.tobj")),
                ]
                if cab_id + FILMS in e.suitable:  # "<body> - windows and trim" is ticked
                    film, acc = _film_files(
                        body, src, f"{pj_root}/{n}_{j}", t, f"{n}_{j}", it.get("SkinName") or n, read
                    )
                    taken = {f.path for f in files}  # bodies of one coach share nothing else
                    files += [f for f in film if f.path not in taken]
                    if acc:  # choosing the paint job puts its films on; other paint jobs drop them
                        unit_attrs.append(("overrides[]", sii_str(acc)))
                unit = ("accessory_paint_job_data", f"{n}_{j}.{t}.paint_job", unit_attrs)
                files.append(OutFile(f"{def_dir}/{n}_{j}.sii", sii_file([unit]).encode()))
                j += 1
    except (OSError, ValueError) as e:
        raise RuntimeError(f"{it.display()}: could not read the coach from the game: {e}") from e
    return files


#: Raise when the baking changes, so cached bakes from older versions are not reused.
BAKE_VERSION = 4


def _baked(body: object, region: object, src: Path, model: str) -> Path:
    """Bake the blueprint ``src`` for one body, cached on disk by image, body and version.

    Baking a 4096x2048 texture takes a few seconds, so the result is kept in the texture cache
    folder and reused while the image and the game data stay the same.
    """
    import hashlib

    from . import coach
    from .appdirs import cache_dir

    st = src.stat()
    cab = body.cab  # type: ignore[attr-defined]
    key = "|".join(
        map(str, (BAKE_VERSION, src.resolve(), st.st_size, st.st_mtime_ns, model, region, len(cab.pmd_data)))
    )
    path = cache_dir() / "bake" / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".png")
    if not path.is_file():
        with Image.open(src) as im:
            img = coach.bake(body, region, im)  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.png")
        img.save(tmp)
        os.replace(tmp, path)
    return path


def _film_files(
    body: object, src: Path, root: str, veh: str, n: str, label: str, read: object
) -> tuple[list[OutFile], str | None]:
    """Window and trim films for one body, showing the blueprint ``src`` (see :mod:`paintbooth.coach`).

    The films come as a window frame accessory (the standard frame plus the films), suitable
    only for the paint job ``n``; the paint job lists it in ``overrides[]``, which swaps it for
    the coach's window frame when the paint job is chosen.

    Returns:
        The files, and the accessory definition's path for the paint job's ``overrides[]`` (None
        when the body has no standard window frame or nothing to cover).
    """
    from . import coach

    size = coach.BLUEPRINT_SIZE
    outside = TextureJob(src, "film", size, "DXT5", None, 1.0)
    inside = TextureJob(src, "film", size, "DXT5", None, coach.GLASS_OPACITY)
    f = coach.films(
        body,  # type: ignore[arg-type]
        f"/{root}/film_glass.mat",
        f"/{root}/film_trim.mat",
        f"/{root}/film_inside.mat",
        f"/{root}/film_glass_core.mat",
        f"/{root}/film_trim_core.mat",
    )
    if f is None:
        return [], None
    out = [
        OutFile(f"{root}/film.dds", job=outside),
        OutFile(f"{root}/film.tobj", tobj_bytes(f"/{root}/film.dds", profile="skin")),
        OutFile(f"{root}/film_inside.dds", job=inside),
        OutFile(f"{root}/film_inside.tobj", tobj_bytes(f"/{root}/film_inside.dds", profile="skin")),
        OutFile(f"{root}/film_glass.mat", coach.film_material(f"/{root}/film.tobj", glossy=False)),
        OutFile(f"{root}/film_trim.mat", coach.film_material(f"/{root}/film.tobj", glossy=True)),
        OutFile(f"{root}/film_inside.mat", coach.film_material(f"/{root}/film_inside.tobj", glossy=False)),
        OutFile(f"{root}/film_glass_core.mat", coach.film_material(f"/{root}/film.tobj", False, core=True)),
        OutFile(f"{root}/film_trim_core.mat", coach.film_material(f"/{root}/film.tobj", True, core=True)),
        OutFile(f"{root}/frame.pmd", f.exterior[0]),
        OutFile(f"{root}/frame.pmg", f.exterior[1]),
    ]
    frame = (read(f.frame_def.lstrip("/")) or b"").decode("utf-8", "replace")  # type: ignore[operator]
    icon = re.search(r'^\s*icon\s*:\s*("[^"]*")', frame, re.M)
    acc: Attrs = [
        ("name", sii_str(f"{label} - windows and trim")),
        ("price", "0"),
        ("unlock", "0"),
        *([("icon", icon.group(1))] if icon else []),
        ("part_type", "factory"),
        ("exterior_model", sii_str(f"/{root}/frame.pmd")),
    ]
    if f.interior is not None:
        out += [
            OutFile(f"{root}/film_inside.pmd", f.interior[0]),
            OutFile(f"{root}/film_inside.pmg", f.interior[1]),
        ]
        acc.append(("interior_model", sii_str(f"/{root}/film_inside.pmd")))
    acc.append(("suitable_for[]", sii_str(f"{n}.{veh}.paint_job")))
    def_path = f"def/vehicle/bus/{veh}/accessory/{coach.FILM_TYPE}/{n}.sii"
    unit = ("accessory_addon_data", f"{n}.{veh}.{coach.FILM_TYPE}", acc)
    out.append(OutFile(def_path, sii_file([unit]).encode()))
    return out, f"/{def_path}"


def _manifest_files(
    project: Ms2Project, mo: Mapping[str, str], base: Path | None, icons: tuple[Path, Path] | None
) -> list[OutFile]:
    """Return manifest.sii, mod_icon.jpg and mod_description.txt.

    ``compatible_versions[]`` is only written for versions the user typed into the project.
    """
    m = project.manifest.vars
    pkg = m.value("GlobalInternalName") or "package_name"
    man: Attrs = [
        ("package_version", sii_str(mo.get("package_version", m.value("package_version")))),
        ("display_name", sii_str(mo.get("display_name", m.value("display_name")))),
        ("author", sii_str(m.value("author"))),
    ]
    if m.bool("multiplayer_optional"):
        man.append(("mp_mod_optional", "true"))
    man += [("category[]", sii_str(c)) for c in m.strings("category")]
    man += [("dlc_dependencies[]", sii_str(d)) for d in m.strings("dlc_dependencies")]
    man += [
        ("compatible_versions[]", sii_str(c.strip())) for c in m.strings("compatible_versions") if c.strip()
    ]
    man += [("icon", sii_str("mod_icon.jpg")), ("description_file", sii_str("mod_description.txt"))]
    files = [OutFile("manifest.sii", sii_file([("mod_package", f".{pkg}", man)]).encode())]
    icon = resolve(m.value("icon"), base) or _defaults(icons)[1]
    files.append(OutFile("mod_icon.jpg", job=TextureJob(icon, "modicon", MOD_ICON_SIZE, "JPG")))
    # the game wants CRLF line ends; normalise first so CRLF text is not doubled
    desc = mo.get("description", m.value("description")).replace("\r\n", "\n")
    files.append(OutFile("mod_description.txt", desc.replace("\n", "\r\n").encode("utf-8")))
    return files


def plan_ms2(
    project: Ms2Project,
    manifest_overrides: Mapping[str, str] | None = None,
    default_icons: tuple[Path, Path] | None = None,
) -> tuple[list[OutFile], list[Problem]]:
    """Validate ``project`` and list the files of its mod.

    Args:
        project: The project to export.
        manifest_overrides: Replaces manifest values for this export only
            (``display_name``, ``package_version``, ``description``).
        default_icons: (skin icon, mod icon) to use when the project chooses none;
            Paintbooth's own default icons when None.

    Returns:
        (files, problems). ``files`` is empty when a problem is an error. Each mod
        path occurs once; later files win (the manifest over files of an
        "Existing mod" item).

    Raises:
        RuntimeError: A template item failed to build (the message names the item).
    """
    mo = manifest_overrides or {}
    probs = validate(project)
    files: list[OutFile] = []
    if any(p.level == "error" for p in probs):
        return files, probs
    base = project.path.parent if project.path else None

    for idx, it in enumerate(project.items):
        if isinstance(it, TemplateItem):
            from .ms2tpl import TemplateError
            from .tplbuild import plan_item

            try:
                tfiles, warns = plan_item(project, it)
            except TemplateError as err:
                raise RuntimeError(f"{it.display()}: {err}") from None
            files += tfiles
            probs += [Problem("warning", idx, "", w) for w in warns]
        elif isinstance(it, AdvancedTruckSkin):
            files += _plan_skin(it, base, default_icons)

    files += _manifest_files(project, mo, base, default_icons)
    # one file per path (case-insensitive, like the game's file system); later entries win
    unique: dict[str, OutFile] = {}
    for f in files:
        unique.pop(f.path.lower(), None)
        unique[f.path.lower()] = f
    return list(unique.values()), probs
