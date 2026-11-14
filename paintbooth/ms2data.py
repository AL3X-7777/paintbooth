"""The user's own Mods Studio 2 installation: its mod templates and data lists.

Paintbooth ships none of Mods Studio 2's templates, 3D models, materials or
lists. Template items (lightbox, windscreen decals, cargo containers, ...)
are built from the files of the Mods Studio 2 copy installed on the same PC,
read at the moment they are needed.

:func:`ci_path` is the one way into that folder: MS2 data uses Windows paths in
any letter case, and the paths come from project files, so it also refuses to
leave the folder it starts from.
"""

from __future__ import annotations

import functools
import logging
import os
import xml.parsers.expat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from .safepath import parts_of
from .xmldom import Elem, parse

log = logging.getLogger(__name__)

GAME_DIR = {"ets2": "ETS2", "ats": "ATS"}

# the template item types Paintbooth can build, in menu order: kind -> (menu label, short help)
FEATURES = {
    "TruckLightbox": ("Truck lightbox", "Your own artwork in the Volvo FH roof lightbox (ETS2)"),
    "TruckWindscreenDecal": ("Truck windscreen decal", "A decal on the windscreen, sold in the truck shop"),
    "TruckDoorWindow": ("Truck door window", "Artwork on the door windows (Scania R/S 2016, ETS2)"),
    "Billboard": ("Billboard", "Replace the roadside billboards with your own images"),
    "CargoContainer2": ("Cargo container V2", "A cargo with your own container skins"),
    "PlayerCompanyLogo": ("Player company logo", "Replace the logos you can pick for your company"),
    "ExistingMod": ("Existing mod", "Pack another mod's files (.scs or .zip) into this mod"),
}

# parsed XML files kept in memory (template.xml, company and cargo lists); keyed by path + mtime
_XML_CACHE_SIZE = 1024


def candidates(settings: Mapping[str, Any] | None = None, hints: list[str] | None = None) -> list[Path]:
    """Folders where Mods Studio 2 may be installed, most likely first, without duplicates.

    Args:
        settings: The app settings; ``ms2_dir`` is the folder the user chose.
        hints: Install folders recorded in the project (see :func:`project_hints`).

    Returns:
        The ``PAINTBOOTH_MS2`` environment folder (if set), the chosen folder, the hints, then the
        usual install locations.
    """
    out: list[Path] = []
    s = settings or {}
    if s.get("ms2_dir"):
        out.append(Path(s["ms2_dir"]))
    for h in hints or []:
        if h:
            out.append(Path(str(PureWindowsPath(h))) if os.name == "nt" else Path(h))
    env = os.environ
    for root in (
        env.get("SystemDrive", "C:") + "\\",
        env.get("ProgramFiles"),
        env.get("ProgramFiles(x86)"),
        env.get("LOCALAPPDATA") and str(Path(env["LOCALAPPDATA"]) / "Programs"),
    ):
        if root:
            out.append(Path(root) / "Mods Studio 2")
    if env.get("PAINTBOOTH_MS2"):
        out.insert(0, Path(env["PAINTBOOTH_MS2"]))
    seen, uniq = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def is_ms2_dir(p: Path | None) -> bool:
    """Return True if ``p`` looks like a Mods Studio 2 installation (has ``Data/ModProjects``)."""
    try:
        return p is not None and (Path(p) / "Data" / "ModProjects").is_dir()
    except OSError:
        return False


def find(settings: Mapping[str, Any] | None = None, hints: list[str] | None = None) -> Path | None:
    """The first of :func:`candidates` that is a Mods Studio 2 installation, or None."""
    for p in candidates(settings, hints):
        if is_ms2_dir(p):
            return p
    return None


def project_hints(root: Elem | None) -> list[str]:
    """Env.ApplicationDirectory values stored in an .ms2 project (where MS2 was installed)."""
    hints: list[str] = []
    if root is None:
        return hints

    def walk(e: Elem) -> None:
        if e.tag == "Variable" and e.get_text("Name") == "Env.ApplicationDirectory":
            v = e.get_text("Value").strip()
            if v and v not in hints:
                hints.append(v)
        for c in e.children:
            walk(c)

    walk(root)
    return hints


def ci_path(base: Path, rel: str) -> Path | None:
    r"""``base/rel`` matched case-insensitively, never leaving ``base``.

    MS2 data uses Windows paths in any letter case, while Linux file systems (and the tests) are
    case-sensitive, so each part is looked up as written first and then by a case-insensitive
    directory scan.

    ``rel`` comes from template and project files, so it is untrusted: a ``..`` part, a drive
    letter or any other part with ``:`` makes the lookup fail, and so does a result that resolves
    outside ``base`` (a symbolic link). Leading separators are allowed and mean ``base`` itself
    (``\Data\ModProjects\...`` is how MS2 writes paths inside its own folder).

    Args:
        base: The folder to look in.
        rel: A relative path, ``/`` or ``\`` separated.

    Returns:
        The existing path, or None if any part is missing or the path is unsafe.
    """
    parts = parts_of(rel)
    if any(x == ".." or ":" in x for x in parts):
        log.warning("refused path %r: it would leave %s", rel, base)
        return None
    p = base
    for part in parts:
        q = p / part
        if q.exists():
            p = q
            continue
        try:
            hit = next((c for c in p.iterdir() if c.name.lower() == part.lower()), None)
        except OSError:
            hit = None
        if hit is None:
            return None
        p = hit
    if parts:
        try:
            inside = p.resolve().is_relative_to(base.resolve())
        except OSError:
            inside = False
        if not inside:
            log.warning("refused path %r: it resolves outside %s", rel, base)
            return None
    return p


def template_dir(ms2: Path, base_folder: str) -> Path | None:
    r"""``\Data\ModProjects\ETS2\Templates\TruckLightbox\`` -> folder inside the MS2 install."""
    return ci_path(ms2, base_folder)


@dataclass
class TemplateDef:
    """One template of the MS2 installation (``Templates/<kind>/template.xml``).

    Attributes:
        kind: The template's ``xsi:type`` (e.g. ``TruckLightbox``).
        name: Display name from the template.
        description: Help text from the template.
        group: The menu group MS2 shows it in.
        folder: The template's folder (models, materials and default images).
        root: The parsed ``template.xml``. Shared with the cache: clone it before changing it.
    """

    kind: str
    name: str
    description: str
    group: str
    folder: Path
    root: Elem = field(repr=False)


@functools.lru_cache(maxsize=_XML_CACHE_SIZE)
def _parse_cached(path: str, mtime_ns: int, size: int) -> Elem:
    """Parse ``path``; the mtime and size are part of the cache key, so edits are picked up."""
    del mtime_ns, size  # cache key only
    return parse(Path(path).read_bytes())


def _load_xml(p: Path) -> Elem | None:
    """The parsed XML file ``p`` (cached), or None if it can't be read or parsed."""
    try:
        st = p.stat()
        return _parse_cached(str(p), st.st_mtime_ns, st.st_size)
    except (OSError, ValueError, IndexError, xml.parsers.expat.ExpatError) as e:
        log.warning("cannot read %s: %s", p, e)
        return None


def clear_cache() -> None:
    """Forget every parsed XML file (tests, or after the MS2 folder changed)."""
    _parse_cached.cache_clear()


def templates(ms2: Path | None, game: str) -> dict[str, TemplateDef]:
    """Kind -> template definition, for every template in MS2's folder for ``game``."""
    out: dict[str, TemplateDef] = {}
    if ms2 is None or not is_ms2_dir(ms2):
        return out
    tdir = ci_path(ms2, f"Data/ModProjects/{GAME_DIR[game]}/Templates")
    if tdir is None:
        return out
    for sub in sorted(tdir.iterdir()):
        x = sub / "template.xml"
        if not x.is_file():
            continue
        root = _load_xml(x)
        if root is None or root.tag != "Template":
            continue
        kind = root.type()
        out[kind] = TemplateDef(
            kind,
            root.get_text("TemplateName") or kind,
            root.get_text("TemplateDescription"),
            root.get_text("TemplateGroup"),
            sub,
            root,
        )
    return out


def template(ms2: Path | None, game: str, kind: str) -> TemplateDef | None:
    """The template of type ``kind`` for ``game``, or None if MS2 doesn't have it."""
    return templates(ms2, game).get(kind)


# ------------------------------------------------------------------ data lists


@dataclass
class Choice:
    """One entry of an MS2 data list (a company, a cargo preset).

    Attributes:
        value: The value stored in the project.
        text: The text shown to the user.
        group: Optional group heading.
        image: Optional preview image path, as MS2 wrote it.
        path: The XML file the entry came from (cargo presets).
    """

    value: str
    text: str
    group: str = ""
    image: str = ""
    path: Path | None = None


def _data_dir(ms2: Path | None, sub: str, game: str) -> Path | None:
    """``Data/ModData/SCS/<sub>/<GAME>`` inside the MS2 install, or None."""
    if ms2 is None or not is_ms2_dir(ms2):
        return None
    return ci_path(ms2, f"Data/ModData/SCS/{sub}/{GAME_DIR[game]}")


def companies(ms2: Path | None, game: str) -> list[Choice]:
    """The game's companies, as listed by MS2 (used for company-logo items)."""
    d = _data_dir(ms2, "Companies", game)
    out = []
    for f in sorted(d.glob("*.xml")) if d else []:
        e = _load_xml(f)
        if e is not None:
            out.append(
                Choice(
                    e.get_text("Value") or f.stem,
                    e.get_text("Text") or f.stem,
                    e.get_text("Group"),
                    e.get_text("Image"),
                )
            )
    return out


def cargo_presets(ms2: Path | None, game: str) -> list[Choice]:
    """The game's own cargo definitions MS2 ships (a starting point for a new cargo)."""
    d = _data_dir(ms2, "Cargo", game)
    return [Choice(f.stem, f.stem.replace("_", " "), path=f) for f in sorted(d.glob("*.xml"))] if d else []


def cargo_preset(c: Choice) -> Elem | None:
    """A private copy of the cargo preset's XML, or None if it can't be read."""
    if c.path is None:
        return None
    e = _load_xml(c.path)
    return e.clone() if e is not None else None
