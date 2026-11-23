r"""The vehicle library: building it from the game, packing it, and installing it with one button.

Mods Studio 2 ships a folder of vehicle XML files that tells it, for every truck and trailer,
which cabins or bodies exist and which accessories take the paint job. Paintbooth's library is
the same idea, made by Paintbooth itself from the game's own files (no Mods Studio 2 data):

* :func:`build` reads a game folder (``def.scs``, ``locale.scs`` and every ``dlc_*.scs``) or a
  single archive and returns :class:`~paintbooth.vehlib.Vehicle` entries for every truck, coach
  and ownable trailer, with real in-game names resolved from the English language files
  (``@@cab_globe@@`` becomes "Globetrotter").
* :func:`write_pack` writes them as one XML file per vehicle plus an ``index.json``, which is
  what ships inside Paintbooth (``paintbooth/data/library``) and on each GitHub release
  (``vehicle-library.zip``).
* :func:`install` is the button in the Vehicles window: it copies the newest pack (the bundled
  one, or a newer one from GitHub when online) into ``Documents\\Paintbooth\\Vehicles\\Library``
  and adds the truck and trailer mods found in the game's mod folder. The user's own files in
  ``Vehicles`` are never touched; only the ``Library`` folder is replaced.

What counts as paintable: for trucks, the accessories SCS's own paint jobs name in ``acc_list``
overrides (exactly the parts the game paints separately); for trailers, the bodies (as
variants, ``body.<item>``) and every accessory item.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import _add_bus_regions, _bus_cabin_name, _Layered, _pretty, units
from .vehlib import Accessory, Variant, Vehicle, save_to_library

log = logging.getLogger(__name__)
Log = Callable[[str], None]

#: Where the library pack ships inside Paintbooth, and where newer packs are downloaded from
#: (the ``packs`` folder of the GitHub repository).
BUNDLED = Path(__file__).resolve().parent / "data" / "library"
PACK_NAME = "vehicle-library.zip"
PACKS_URL = "https://raw.githubusercontent.com/AL3X-7777/paintbooth/main/packs/"
PACK_URL = PACKS_URL + PACK_NAME
#: Community vehicle definitions (Mods Studio 2 format, made by the community); download only.
COMMUNITY_NAME = "community-vehicles.zip"
COMMUNITY_URL = PACKS_URL + COMMUNITY_NAME
#: The installed library's folder inside the user's vehicle folder.
LIBRARY_FOLDER = "Library"

_KEY_RE = re.compile(r'^\s*key\[\]\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
_VAL_RE = re.compile(r'^\s*val\[\]\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
_TOKEN_RE = re.compile(r"@@([^@]+)@@")


# ---------------------------------------------------------------------------------- names
class Localizer:
    """The game's English strings: resolves ``@@key@@`` tokens in names.

    The strings live in ``locale/en_us/*.sii`` and ``*.sui`` files as parallel ``key[]`` and
    ``val[]`` arrays (``locale.scs`` for the base game, each DLC may add its own).
    """

    def __init__(self, strings: dict[str, str] | None = None):
        self.strings = strings or {}

    @classmethod
    def from_source(cls, src: _Layered) -> Localizer:
        """Read every English string file of a layered source."""
        strings: dict[str, str] = {}
        for path, reader in src.files("locale/en_us").items():
            if not path.endswith((".sii", ".sui")):
                continue
            try:
                text = reader(path)
            except (OSError, KeyError, ValueError) as e:
                log.debug("skipping %s: %s", path, e)
                continue
            keys, vals = _KEY_RE.findall(text), _VAL_RE.findall(text)
            strings.update(zip(keys, vals, strict=False))
        return cls(strings)

    def __call__(self, text: str, fallback: str = "") -> str:
        """Return ``text`` with every ``@@key@@`` replaced; ``fallback`` if a key is unknown."""
        if "@@" not in text:
            return text or fallback
        missing = False

        def one(m: re.Match[str]) -> str:
            nonlocal missing
            val = self.strings.get(m.group(1))
            if val is None:
                missing = True
                return ""
            return val

        out = _TOKEN_RE.sub(one, text).strip()
        return fallback if missing or not out else out


# ---------------------------------------------------------------------------------- scanning
@dataclass
class _Info:
    """What the build learned about one vehicle (truck, bus or trailer)."""

    brand: str = ""
    series: str = ""
    variants: dict[str, str] = field(default_factory=dict)  # id -> raw name
    models: dict[str, str] = field(default_factory=dict)  # cabin id -> model (buses)
    acc_names: dict[str, str] = field(default_factory=dict)  # "<type>.<item>" -> raw name
    acc_used: Counter[str] = field(default_factory=Counter)  # values SCS paint jobs list

    def add(self, kind: str, vid: str, rel: str, text: str) -> None:
        """Record the units of one SII file below the vehicle's folder (``rel``)."""
        for cls, uname, attrs in units(text):
            parts = uname.split(".")
            nm = (attrs.get("name") or [""])[0]
            if cls in ("accessory_truck_data", "accessory_trailer_data"):
                info = attrs.get("info[]", [])
                self.brand = info[0] if info else self.brand
                self.series = info[1] if len(info) > 1 else self.series
            elif cls == "accessory_cabin_data":
                self.variants[uname] = nm
                model = (attrs.get("model") or [""])[0]
                if model:
                    self.models[uname] = model
            elif cls == "accessory_trailer_body_data":
                self.variants[f"body.{parts[0]}"] = nm
            elif cls == "simple_paint_job_data" and rel.startswith("paint_job/"):
                for a in attrs.get("acc_list[]", []):
                    self.acc_used[a] += 1
            elif cls.startswith("accessory_") and len(parts) >= 3 and ".".join(parts[1:-1]) == vid:
                # accessory units are "<item>.<vehicle id>.<type>"; acc_list values "<type>.<item>"
                if rel.startswith("accessory/") or kind == "truck":
                    self.acc_names[f"{parts[-1]}.{parts[0]}"] = nm
                if kind == "trailer_owned" and rel.startswith("accessory/"):
                    self.acc_used[f"{parts[-1]}.{parts[0]}"] += 0


_KIND_TYPE = {"truck": "NewTrucks", "bus": "Buses", "trailer_owned": "Trailers"}


def _vehicle(kind: str, vid: str, info: _Info, game: str, loc: Localizer, source: str) -> Vehicle:
    """Turn what was scanned into a library entry, with names resolved."""
    from .templates import known_name

    brand_id, _, model_id = vid.partition(".")
    brand = loc(info.brand) if info.brand else ""
    series = loc(info.series) if info.series else ""
    known = known_name(vid, game) if game in ("ets2", "ats") else ""
    if not brand:
        brand = known.split(" ", 1)[0] if known.lower().startswith(brand_id.lower()) else _pretty(brand_id)
    if brand.lower() == "scs":
        brand = "SCS"
    if known and not info.brand and known.lower().startswith(brand.lower()):
        brand = known[: len(brand)]  # "DAF", not "Daf"
    from_known = known[len(brand) :].strip() if known.lower().startswith(brand.lower()) else known
    name = series or from_known
    v = Vehicle(
        vid, game, brand, name or _pretty(model_id), "SCS Software", "Paintbooth library", source=source
    )
    v.vehicle_type = _KIND_TYPE[kind]
    for x_id, raw in sorted(info.variants.items()):
        short = x_id.split(".", 1)[0] if kind != "trailer_owned" else x_id.split(".", 1)[1]
        fallback = _bus_cabin_name(x_id) if kind == "bus" else _pretty(short)
        # coach bodies: the game's names ("L 13.9m") say less than ours ("13.9 m high-deck, 6 wheels")
        label = fallback if kind == "bus" or not raw else loc(raw, fallback)
        v.variants.append(Variant(x_id, label, model=info.models.get(x_id)))
    for value in sorted(info.acc_used):
        typ, _, item = value.partition(".")
        label = loc(info.acc_names.get(value, ""), _pretty(item))
        v.accessories.append(Accessory(value, label, _pretty(typ)))
    trailer = kind == "trailer_owned"
    _disambiguate([(x.name, x, x.id.split(".", 1)[trailer]) for x in v.variants])
    _disambiguate([(f"{a.group}|{a.name}", a, a.value.partition(".")[2]) for a in v.accessories])
    return v


def _disambiguate(items: list[tuple[str, Variant | Accessory, str]]) -> None:
    """Add the item id to names that occur more than once ("Curtainsider (curtain_136)").

    The game often gives several bodies or parts the same shop name (they differ in length or
    axles); in a tick list they would be impossible to tell apart.
    """
    counts = Counter(key for key, _, _ in items)
    for key, obj, short in items:
        if counts[key] > 1:
            obj.name = f"{obj.name} ({short})"


def build(source: Path, game: str = "ets2", log_line: Callable[[str], object] | None = None) -> list[Vehicle]:
    """Build library entries for every truck, coach and ownable trailer in ``source``.

    Args:
        source: The game folder (``def.scs``, ``locale.scs`` and ``dlc_*.scs`` are read), one
            archive (a mod, ZIP or HashFS), or an extracted folder.
        game: ``"ets2"`` or ``"ats"``, recorded in the entries.
        log_line: Receives one line per vehicle.
    """
    found: dict[tuple[str, str], _Info] = defaultdict(_Info)
    with _Layered(Path(source)) as src:
        loc = Localizer.from_source(src)
        files: dict[str, Callable[[str], str]] = {}
        for kind in _KIND_TYPE:
            files.update(src.files(f"def/vehicle/{kind}"))
        for path, reader in files.items():
            m = re.match(r"def/vehicle/(truck|bus|trailer_owned)/([^/]+)/(.+)\.sii$", path)
            if not m:
                continue
            kind, vid, rel = m.groups()
            try:
                text = reader(path)
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
                log.debug("unreadable %s: %s", path, e)
                continue
            found[(kind, vid)].add(kind, vid, rel, text)
    out = []
    for (kind, vid), info in sorted(found.items()):
        if not info.variants:
            continue  # accessories only (a tuning pack adding to another vehicle), or not a vehicle
        v = _vehicle(kind, vid, info, "ets2" if kind == "bus" else game, loc, str(source))
        if kind == "bus":
            _add_bus_regions(v, Path(source))
        out.append(v)
        if log_line:
            log_line(
                f"{vid:32} {kind:13} variants: {len(v.variants):2}  paintable parts: {len(v.accessories):3}"
            )
    return out


# ---------------------------------------------------------------------------------- packs
def write_pack(vehicles: list[Vehicle], out_dir: Path, version: str, game_version: str = "") -> Path:
    """Write a library pack: one XML file per vehicle and ``index.json``.

    Args:
        vehicles: The entries (from :func:`build`).
        out_dir: The pack folder; replaced if it exists.
        version: The pack's version (a date such as ``2026.11.14`` is fine; newer must sort higher).
        game_version: The game version it was built from, for the record.

    Returns:
        The folder written.
    """
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    names = []
    for v in vehicles:
        v.source = ""
        p = save_to_library(v, out_dir, f"Paintbooth vehicle library {version}")
        names.append(p.name)
    index = {"version": version, "game_version": game_version, "vehicles": len(names), "files": sorted(names)}
    (out_dir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return out_dir


def pack_version(folder: Path) -> str:
    """The version recorded in a pack's ``index.json`` ("" if none or unreadable)."""
    try:
        return str(json.loads((folder / "index.json").read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def _download(timeout: float = 20.0, url: str = PACK_URL) -> Path | None:
    """Download a pack (a flat zip of XMLs and ``index.json``) into a temporary folder.

    Returns None when offline or the pack is missing or damaged.
    """
    tmp = Path(tempfile.mkdtemp(prefix="paintbooth-library-"))
    try:
        if not url.startswith(PACKS_URL):
            raise ValueError(f"not a Paintbooth pack URL: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Paintbooth-library"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https:// URL)
            (tmp / PACK_NAME).write_bytes(r.read(64 * 1024 * 1024))
        with zipfile.ZipFile(tmp / PACK_NAME) as z:
            for info in z.infolist():
                name = Path(info.filename).name  # flat pack: no folders, so no path tricks
                if name.endswith((".xml", ".json")) and not info.is_dir():
                    (tmp / "pack").mkdir(exist_ok=True)
                    (tmp / "pack" / name).write_bytes(z.read(info))
        return tmp / "pack" if (tmp / "pack" / "index.json").is_file() else None
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
        log.info("no library download: %s", e)
        return None


@dataclass
class InstallResult:
    """What :func:`install` did."""

    version: str = ""
    downloaded: bool = False
    vehicles: int = 0
    community: int = 0
    mods: int = 0
    mod_errors: list[str] = field(default_factory=list)


def install(
    vehicles_dir: Path, mod_dir: Path | None, online: bool = True, log_line: Log | None = None
) -> InstallResult:
    r"""Install the library into ``vehicles_dir/Library``, plus the truck mods in ``mod_dir``.

    The newest of the bundled pack and (when ``online``) the GitHub pack is used. Online, the
    community pack is downloaded into ``Library/Community`` too; offline, the community files
    already installed are kept. The ``Library`` folder is replaced as a whole; nothing else in
    ``vehicles_dir`` changes.

    Args:
        vehicles_dir: The user's vehicle folder (``Documents\\Paintbooth\\Vehicles``).
        mod_dir: The game's mod folder, or None to skip mods.
        online: Try GitHub for a newer pack.
        log_line: Receives progress lines.
    """
    res = InstallResult()
    pack = BUNDLED
    res.version = pack_version(BUNDLED)
    if online:
        fresh = _download()
        if fresh is not None and pack_version(fresh) > res.version:
            pack, res.version, res.downloaded = fresh, pack_version(fresh), True
    target = vehicles_dir / LIBRARY_FOLDER
    tmp = vehicles_dir / (LIBRARY_FOLDER + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "SCS").mkdir(parents=True)
    for f in pack.glob("*.xml"):
        shutil.copy2(f, tmp / "SCS" / f.name)
        res.vehicles += 1
    shutil.copy2(pack / "index.json", tmp / "SCS" / "index.json")
    community = _download(url=COMMUNITY_URL) if online else None
    if community is None and (target / "Community").is_dir():
        community = target / "Community"  # offline: keep the community files from last time
    if community is not None:
        (tmp / "Community").mkdir()
        for f in community.glob("*.*"):
            if f.suffix in (".xml", ".json"):
                shutil.copy2(f, tmp / "Community" / f.name)
                res.community += f.suffix == ".xml"
    if mod_dir is not None and mod_dir.is_dir():
        res.mods = _add_mods(mod_dir, tmp / "Mods", res.mod_errors, log_line)
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    return res


def _add_mods(mod_dir: Path, out: Path, errors: list[str], log_line: Log | None) -> int:
    """Write library entries for the truck, coach and trailer mods in ``mod_dir``; return how many."""
    count = 0
    for archive in sorted([*mod_dir.glob("*.scs"), *mod_dir.glob("*.zip")]):
        try:
            vehicles = build(archive, "ets2")
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
            errors.append(f"{archive.name}: {e}")
            continue
        for v in vehicles:
            v.author, v.definition_by = archive.stem, "Paintbooth library (from your mod folder)"
            v.source = ""
            out.mkdir(parents=True, exist_ok=True)
            save_to_library(v, out, f"From the mod {archive.name}")
            count += 1
            if log_line:
                log_line(f"mod {archive.name}: {v.id}")
    return count
