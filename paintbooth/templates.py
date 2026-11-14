r"""Paint job templates: import a template pack and find the folder for a vehicle.

The template pack is a folder tree of PSD / PNG files with one folder per vehicle (the exact
naming differs between packs and game versions). Paintbooth extracts a pack into
``Documents\Paintbooth\Templates\<game>`` and matches vehicles to folders by name: the words of
the vehicle's internal name, brand and model are compared with the folder names. A wrong or
missing match can be fixed by choosing the folder once; that choice is remembered per vehicle.

Template packs are downloaded from forum links, so their member names are untrusted: every output
path goes through :mod:`paintbooth.safepath` before anything is written.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import unicodedata
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import appdirs
from .safepath import UnsafePathError, parts_of, safe_join

if TYPE_CHECKING:
    from .vehlib import Vehicle

log = logging.getLogger(__name__)

IMAGE_EXT = {".psd", ".png", ".jpg", ".jpeg", ".tga", ".dds", ".tif", ".tiff"}
# sub-folders that only sort a vehicle's files by format; they are folded into the vehicle folder
FORMAT_DIRS = {"psd", "psds", "png", "pngs", "jpg", "dds", "tga", "templates", "template", "files"}
# words that say nothing about which vehicle a folder is for
STOP = {
    "scs",
    "the",
    "by",
    "and",
    "for",
    "of",
    "template",
    "templates",
    "psd",
    "png",
    "dds",
    "tga",
    "jpg",
    "paintjob",
    "paint",
    "job",
    "ets",
    "ets2",
    "ats",
    "ver",
    "version",
    "game",
    "dlc",
    "mod",
    "pack",
    "skin",
    "skins",
    "all",
    "files",
    "v",
}

ProgressFn = Callable[[int, int, str], Any]


# ------------------------------------------------------------------------------------------ text
def _ascii(s: str) -> str:
    """Fold accents and a few ligatures to plain ASCII ('Kögel' -> 'Kogel')."""
    s = s.replace("ß", "ss").replace("Æ", "ae").replace("æ", "ae").replace("Ø", "o").replace("ø", "o")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def tokens(text: str) -> list[str]:
    """Split a name into lower-case comparison words, without filler words.

    'Kögel Cargo_137' -> ['kogel', 'cargo', '137']; 'fh16' -> ['fh', '16']. German "oe"/"ue"
    spellings are folded too, so 'Koegel' and 'Kögel' match.
    """
    t = _ascii(text).lower().replace("oe", "o").replace("ue", "u") if text else ""
    parts = re.findall(r"[a-z]+|\d+", t)
    return [p for p in parts if p not in STOP]


def squash(text: str) -> str:
    """The comparison words of ``text`` joined together ('Scania R 2009' -> 'scaniar2009')."""
    return "".join(tokens(text))


# ------------------------------------------------------------------------------------------ index
@dataclass
class TemplateDir:
    """One vehicle's templates inside the templates root.

    Attributes:
        path: The vehicle's folder (format sub-folders like PSD/ PNG/ are folded into it).
        rel: The folder relative to the templates root, '/'-separated. For a flat folder holding
            many vehicles' files, one extra entry per file stem uses ``<folder>/<stem>``.
        files: The template images that belong to this entry.
    """

    path: Path
    rel: str
    files: list[Path]

    @property
    def psds(self) -> list[Path]:
        """The Photoshop files, shallowest first, then by name."""
        return sorted(
            (f for f in self.files if f.suffix.lower() == ".psd"),
            key=lambda p: (len(p.parts), p.name.lower()),
        )


# templates root -> (folder signature, index). Bounded: there is one root per game, plus the odd
# test or custom folder, so a handful of entries is plenty.
_INDEX_CACHE_SIZE = 8
_cache: OrderedDict[str, tuple[tuple[Any, ...], list[TemplateDir]]] = OrderedDict()


def _signature(root: Path) -> tuple[Any, ...]:
    """A cheap change marker for ``root``: its mtime plus the names and mtimes of its children."""
    try:
        st = root.stat()
        kids = tuple(sorted((c.name, c.stat().st_mtime_ns) for c in root.iterdir()))
    except OSError:
        return ()
    return (st.st_mtime_ns, kids)


def _group_images(root: Path) -> dict[Path, list[Path]]:
    """Every template image below ``root``, grouped by the vehicle folder it belongs to."""
    groups: dict[Path, list[Path]] = {}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMAGE_EXT:
            d = f.parent
            # fold format folders (".../Scania R/PSD/x.psd") into the vehicle folder
            while d != root and d.name.lower() in FORMAT_DIRS:
                d = d.parent
            if d == root:
                d = f.parent
            groups.setdefault(d, []).append(f)
    return groups


def index(game: str, root: Path | None = None) -> list[TemplateDir]:
    """List the vehicle template folders of ``game`` (or of ``root``), cached until it changes.

    Args:
        game: "ets2" or "ats"; selects ``Templates/<game>`` when ``root`` is not given.
        root: A templates folder to index instead.

    Returns:
        The template entries, sorted by folder.
    """
    root = root or appdirs.templates_dir(game)
    if not root.is_dir():
        return []
    sig = _signature(root)
    key = str(root)
    hit = _cache.get(key)
    if hit is not None and hit[0] == sig:
        _cache.move_to_end(key)
        return hit[1]
    out = []
    for d, fs in sorted(_group_images(root).items()):
        rel = d.relative_to(root).as_posix()
        out.append(TemplateDir(d, rel, sorted(fs)))
        stems: dict[str, list[Path]] = {}
        for f in fs:
            stems.setdefault(
                squash(re.sub(r"\b(psd|png|template|uv|layout)\b", "", f.stem, flags=re.IGNORECASE)), []
            ).append(f)
        if len(stems) > 4:  # a flat folder with templates for many vehicles: also offer each file name
            for _stem, group in sorted(stems.items()):
                out.append(TemplateDir(d, f"{rel}/{group[0].stem}", sorted(group)))
    _cache[key] = (sig, out)
    _cache.move_to_end(key)
    while len(_cache) > _INDEX_CACHE_SIZE:
        _cache.popitem(last=False)
    return out


def clear_cache() -> None:
    """Forget every cached index (after an import, or in tests)."""
    _cache.clear()


# ------------------------------------------------------------------------------------------ matching
@functools.lru_cache(maxsize=1)
def _names() -> dict[str, dict[str, str]]:
    """The bundled ``data/vehicle_names.json``: game -> vehicle id -> in-game name (read once)."""
    try:
        data = json.loads((Path(__file__).parent / "data" / "vehicle_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("vehicle_names.json is missing or broken; matching by internal names only")
        return {}
    return data if isinstance(data, dict) else {}


def known_name(vid: str, game: str = "") -> str:
    """In-game display name of a vehicle (the names template packs are usually organised by).

    Args:
        vid: The vehicle's internal name, e.g. ``scania.r_2016``.
        game: "ets2" or "ats" to look only there; anything else looks in both.

    Returns:
        The display name, or "" when the vehicle is not in the bundled list.
    """
    names = _names()
    for g in [game] if game in ("ets2", "ats") else ["ets2", "ats"]:
        n = names.get(g, {}).get(vid)
        if n:
            return n
    return ""


def vehicle_names(v: Vehicle) -> list[str]:
    """The different ways a vehicle may be named: internal name, in-game name, the XML's name."""
    out: list[str] = []
    for src in (
        v.id,
        known_name(v.id, getattr(v, "game", "")),
        getattr(v, "vehicle_name", "").replace("|", " "),
        f"{v.brand} {v.name}" if v.brand and v.brand.lower() not in (v.name or "").lower() else v.name,
    ):
        if src and tokens(src) and src not in out:
            out.append(src)
    return out


def _f1(name_tokens: list[str], folder_tokens: list[str]) -> float:
    """F1 score of two word lists, weighting each word by its length (substring matches count)."""
    if not name_tokens or not folder_tokens:
        return 0.0
    vs, fs = "".join(name_tokens), "".join(folder_tokens)
    rec = sum(len(w) for w in name_tokens if w in fs) / sum(len(w) for w in name_tokens)
    prec = sum(len(w) for w in folder_tokens if w in vs) / sum(len(w) for w in folder_tokens)
    return 0.0 if rec + prec == 0 else 2 * rec * prec / (rec + prec)


def score(v: Vehicle, td: TemplateDir) -> float:
    """0..1: how well a template folder's name fits the vehicle (best over the vehicle's names).

    The last folder name counts fully, the last two together at 95 %. A "trailer" folder loses a
    little for a truck, and a folder whose name is exactly another vehicle's name loses a lot
    (so "Scania R" is not picked for the "Scania R 2009").
    """
    comps = [c for c in PurePosixPath(td.rel).parts if c.lower() not in FORMAT_DIRS]
    if not comps:
        return 0.0
    last = tokens(comps[-1])
    two = tokens(" ".join(comps[-2:]))
    best = 0.0
    for name in vehicle_names(v):
        nt = tokens(name)
        best = max(best, _f1(nt, last), 0.95 * _f1(nt, two))
    trailer_hint = any(t in ("trailer", "trailers") for t in tokens(td.rel.replace("/", " ")))
    if trailer_hint and not getattr(v, "is_trailer", False):
        best -= 0.1
    names_map, ids_map = _owners()
    raw_last = comps[-1].strip().lower()
    owner = ids_map.get(raw_last) if raw_last in ids_map else names_map.get("".join(last))
    if owner and v.id not in owner:
        best -= 0.3  # the folder is exactly the name of a different vehicle ("Scania R" vs "Scania R 2009")
    elif owner:
        best += 0.05
    return best


@functools.lru_cache(maxsize=1)
def _owners() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(squashed known name -> vehicle ids, internal name -> vehicle ids), built once."""
    by_name: dict[str, set[str]] = {}
    by_id: dict[str, set[str]] = {}
    for g in ("ets2", "ats"):
        for vid, name in _names().get(g, {}).items():
            if squash(name):
                by_name.setdefault(squash(name), set()).add(vid)
            by_id.setdefault(vid.lower(), set()).add(vid)
    return by_name, by_id


def candidates(v: Vehicle, game: str, limit: int = 8) -> list[tuple[float, TemplateDir]]:
    """The ``limit`` best-scoring template folders for ``v``, best first."""
    scored = [(score(v, td), td) for td in index(game)]
    scored.sort(key=lambda x: (-x[0], -len(x[1].psds), x[1].rel))
    return scored[:limit]


def find(v: Vehicle | None, game: str, overrides: dict[str, str] | None = None) -> TemplateDir | None:
    """The template folder for a vehicle: the user's remembered choice, else the best name match.

    Args:
        v: The vehicle, or None.
        game: "ets2" or "ats".
        overrides: Remembered choices, ``"<game>:<vehicle id>" -> folder path``.

    Returns:
        The folder, or None when nothing fits well enough (score below 0.6).
    """
    if v is None:
        return None
    root = appdirs.templates_dir(game)
    key = f"{game}:{v.id}"
    if overrides and overrides.get(key):
        p = Path(overrides[key])
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXT)
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = p.name
            return TemplateDir(p, rel, files)
    best = candidates(v, game, 1)
    if best and best[0][0] >= 0.6:
        return best[0][1]
    return None


# ------------------------------------------------------------------------------------------ import
def guess_game(name: str) -> str | None:
    """Guess the game from a pack's file name ("ETS2 templates.zip" -> "ets2")."""
    n = name.lower()
    if "ets" in n or "euro" in n:
        return "ets2"
    if "ats" in n or "american" in n:
        return "ats"
    return None


def _zip_name(info: zipfile.ZipInfo) -> str:
    """The member name, with mis-decoded UTF-8 repaired.

    Zip tools often store UTF-8 names without the UTF-8 flag; Python then reads them as cp437
    ('Kögel' -> 'K├╢gel'). Undo that when the bytes are valid UTF-8.
    """
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            return name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return name


def _member_parts(name: str) -> list[str]:
    """The usable path parts of a pack member name.

    Root separators, ``.``, ``..`` and anything with a ``:`` (drive letters, NTFS streams) are
    dropped, as packs made on Windows sometimes contain ``C:`` prefixes. The result is joined with
    :func:`safepath.safe_join` afterwards, which is the actual guarantee.
    """
    return [p for p in parts_of(name) if p != ".." and ":" not in p]


def _import_folder(src: Path, dest: Path, progress: ProgressFn | None, cancel: Callable[[], bool]) -> int:
    """Copy the template files of folder ``src`` into ``dest``; see :func:`import_pack`."""
    written = 0
    # symbolic links are skipped: a pack folder should not pull in files from elsewhere
    files = [f for f in src.rglob("*") if f.is_file() and not f.is_symlink()]
    for i, f in enumerate(files):
        if cancel():
            break
        try:
            out = safe_join(dest, f.relative_to(src).as_posix())
        except UnsafePathError as e:
            log.warning("skipped template file %s: %s", f, e)
            continue
        if not (out.exists() and out.stat().st_size == f.stat().st_size):
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            written += 1
        if progress and i % 20 == 0:
            progress(i + 1, len(files), f.name)
    return written


def _import_zip(src: Path, dest: Path, progress: ProgressFn | None, cancel: Callable[[], bool]) -> int:
    """Extract the template files of ZIP ``src`` into ``dest``; see :func:`import_pack`."""
    written = 0
    with zipfile.ZipFile(src) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = {id(i): _member_parts(_zip_name(i)) for i in infos}
        # drop a single top-level folder that just repeats the pack name
        tops = {names[id(i)][0] for i in infos if names[id(i)]}
        strip = len(tops) == 1 and all(len(names[id(i)]) > 1 for i in infos)
        for n, info in enumerate(infos):
            if cancel():
                break
            parts = names[id(info)][1:] if strip else names[id(info)]
            if not parts:
                continue
            try:
                out = safe_join(dest, "/".join(parts))
            except UnsafePathError as e:
                log.warning("skipped pack member %r: %s", info.filename, e)
                continue
            if not (out.exists() and out.stat().st_size == info.file_size):
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fi, open(out, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
                written += 1
            if progress and n % 10 == 0:
                progress(n + 1, len(infos), parts[-1])
    return written


def import_pack(
    src: Path,
    game: str,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] = lambda: False,
) -> int:
    """Extract a template pack (.zip, or copy a folder) into ``Templates/<game>``.

    Existing files with the same size are skipped, so importing again is quick. Member names that
    would land outside the templates folder (absolute, ``..``, drive letters, UNC paths, symbolic
    links pointing out) are skipped.

    Args:
        src: The pack: a .zip file or a folder.
        game: "ets2" or "ats".
        progress: Called as ``progress(done, total, current_name)`` now and then, and with
            ``(1, 1, "")`` at the end.
        cancel: Polled before each file; returning True stops the import.

    Returns:
        The number of files written.
    """
    dest = appdirs.templates_dir(game)
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        written = _import_folder(src, dest, progress, cancel)
    else:
        written = _import_zip(src, dest, progress, cancel)
    clear_cache()
    if progress:
        progress(1, 1, "")
    return written


# ------------------------------------------------------------------------------------------ PSD names
def safe_name(s: str) -> str:
    """``s`` made usable as a Windows file name (reserved characters become ``_``)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    return s or "Untitled"


# Where to download the paint job template packs (Carsmaniac's complete truck & trailer packs,
# shared on the SCS forum). Downloading needs internet; importing works offline.
TEMPLATE_PACKS = {
    "ats": ("ATS truck & trailer templates", "https://forum.scssoft.com/viewtopic.php?t=272386"),
    "ets2": ("ETS2 truck & trailer templates", "https://forum.scssoft.com/viewtopic.php?t=288778"),
}
