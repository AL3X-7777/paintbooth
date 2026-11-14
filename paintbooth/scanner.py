"""Generate vehicle definitions from the game's own data.

Two scanners live here:

* :func:`scan` (``paintbooth scan``) writes vehicle XML for the ``project.xml`` pipeline
  (:mod:`paintbooth.model`). Its input is a folder that contains ``def/vehicle/...`` (the
  base game's ``def.scs`` extracted with SCS's Game Archive Extractor, or an unpacked
  mod truck), or a ZIP-format ``.scs``/``.zip`` file (most mod trucks). HashFS archives
  (the base game's format) must be extracted first; :class:`HashFsArchiveError` says so.

  For every truck / ownable trailer it records the cabins and the ``acc_list``
  override groups SCS's own paint jobs use. Those groups become ``<slot>`` entries,
  so the names are copied from real game data rather than guessed.

* :func:`scan_vehicles` (GUI: Tools > Vehicles > Scan) returns
  :class:`paintbooth.vehlib.Vehicle` entries for the vehicle library. It also reads
  HashFS archives directly, including a whole game folder (``def.scs`` + ``dlc_*.scs``).

SII parsing is deliberately simple (regular expressions over text units); encrypted or
binary SII files are counted as unreadable and skipped.
"""

from __future__ import annotations

import functools
import logging
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from .names import TOKEN_MAX, TOKEN_RE

if TYPE_CHECKING:
    from .hashfs import HashFs
    from .vehlib import Vehicle

log = logging.getLogger(__name__)

# "class : name { body }" - one SII unit; attributes are "key: value" lines of the body
UNIT_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*([\w.]+)\s*\{(.*?)\}", re.S)
ATTR_RE = re.compile(r"^\s*([\w\[\]]+)\s*:\s*(.+?)\s*$", re.M)
# magic numbers of SII files that are not plain text (encrypted, binary SII, 3nK-scrambled)
_BINARY_SII = (b"ScsC", b"BSII", b"3nK\x01")
_HASHFS_MAGIC = b"SCS#"

Reader = Callable[[str], str]
Listing = Callable[[str], list[str]]
Log = Callable[[str], object]


class HashFsArchiveError(ValueError):
    """The input is a HashFS archive, which :class:`Source` cannot read (extract it first)."""


def _decode_sii(data: bytes) -> str:
    """Return SII text; raise ValueError for encrypted or binary SII."""
    if data[:4] in _BINARY_SII:
        raise ValueError("encrypted or binary SII")
    return data.decode("utf-8", errors="replace")


class Source:
    """Read-only view of a folder or a ZIP archive, with forward-slash member names.

    Use it as a context manager (or call :meth:`close`) so a ZIP file is not kept open,
    which on Windows would lock the mod file.

    Raises:
        HashFsArchiveError: ``path`` is a HashFS archive.
        zipfile.BadZipFile: ``path`` is a file but not a ZIP.
    """

    def __init__(self, path: Path):
        self.path = path
        self.zip: zipfile.ZipFile | None = None
        if path.is_file():
            with open(path, "rb") as fh:
                magic = fh.read(4)
            if magic == _HASHFS_MAGIC:
                raise HashFsArchiveError(
                    f"{path.name} is a HashFS archive (base game / DLC format).\n"
                    "Extract it first (SCS's Game Archive Extractor from the modding wiki, or\n"
                    "sk-zk's Extractor), then scan the extracted folder:  paintbooth scan <folder>"
                )
            self.zip = zipfile.ZipFile(path)
            # member names are used as given: they are only matched and read, never written to disk
            self.names = [
                n.replace("\\", "/").lstrip("/") for n in self.zip.namelist() if not n.endswith("/")
            ]
        else:
            self.names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()]
        self.names_set = set(self.names)

    def close(self) -> None:
        """Close the ZIP file, if any."""
        if self.zip is not None:
            self.zip.close()
            self.zip = None

    def __enter__(self) -> Source:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def read(self, name: str) -> str:
        """Return SII file ``name`` as text.

        Raises:
            ValueError: The file is encrypted or binary SII.
            OSError, KeyError: The file cannot be read.
        """
        data = self.zip.read(name) if self.zip else (self.path / name).read_bytes()
        return _decode_sii(data)

    def list(self, prefix: str, suffix: str = "") -> list[str]:
        """Return the member names that start with ``prefix`` and end with ``suffix``."""
        return [n for n in self.names if n.startswith(prefix) and n.endswith(suffix)]


def strip_comments(text: str) -> str:
    """Remove SII comments (``/* */``, ``//`` and ``#``), keeping ``#`` and ``//`` inside strings."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        in_str = False
        cut = line
        for i, ch in enumerate(line):
            if ch == '"':
                in_str = not in_str
            elif not in_str and (ch == "#" or line.startswith("//", i)):
                cut = line[:i]
                break
        out.append(cut)
    return "\n".join(out)


def units(text: str) -> Iterator[tuple[str, str, defaultdict[str, list[str]]]]:
    """Yield (class, unit name, attributes) for every unit of an SII text.

    Attribute values are unquoted; each key maps to all its values in order (array
    attributes such as ``acc_list[]`` occur many times).
    """
    for cls, name, body in UNIT_RE.findall(strip_comments(text)):
        attrs: defaultdict[str, list[str]] = defaultdict(list)
        for k, v in ATTR_RE.findall(body):
            attrs[k].append(v.strip().strip('"'))
        yield cls, name, attrs


def _slot_name(accs: list[str], used: set[str]) -> str:
    """Return a unique, valid slot id named after the first accessory of a group; add it to ``used``."""
    base = None
    for a in accs:
        parts = a.split(".")
        cand = [p for p in parts if p not in ("cabin",)] or parts
        base = re.sub(r"[^a-z0-9_]", "_", cand[-1].lower())[:TOKEN_MAX]
        break
    base = base or "slot"
    name, n = base, 1
    while name in used or not TOKEN_RE.match(name):
        n += 1
        name = (base[:9] + f"_{n}")[:TOKEN_MAX]
    used.add(name)
    return name


class _VehicleScan:
    """What :func:`scan` learned about one vehicle from its SII files."""

    def __init__(self, vid: str):
        self.name = vid
        self.cabins: dict[str, str] = {}
        self.groups: Counter[tuple[str, ...]] = Counter()
        self.parts: Counter[str] = Counter()
        self.problems = 0

    def add(self, rel: str, text: str) -> None:
        """Record the units of one SII file (``rel`` is its path below the vehicle folder)."""
        for cls, uname, attrs in units(text):
            if cls == "accessory_truck_data" and attrs.get("name"):
                nm = attrs["name"][0]
                if not nm.startswith("@@"):  # "@@key@@" is a localisation key, not a name
                    self.name = nm
            elif cls == "accessory_cabin_data":
                self.cabins[uname.split(".")[0]] = (attrs.get("name") or [""])[0]
            elif cls == "simple_paint_job_data" and rel.startswith("paint_job/"):
                accs = tuple(attrs.get("acc_list[]", []))
                if accs:
                    self.groups[accs] += 1
                    self.parts.update(accs)

    def cab_groups(self) -> list[tuple[str, ...]]:
        """Return the override groups that paint a cabin (they become slots)."""
        tokens = set(self.cabins)
        return [
            g
            for g in self.groups
            if any("cabin" in a.split(".") or any(p in tokens for p in a.split(".")) for a in g)
        ]


def _vehicle_xml(vid: str, kind: str, game: str, source_name: str, info: _VehicleScan) -> str:
    """Return the vehicle XML file text for one scanned vehicle."""
    cab_groups = info.cab_groups()
    other_accs = sorted(a for a in info.parts if not any(a in g for g in cab_groups))
    used = {"default"}
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f"<!-- Generated by 'paintbooth scan' from {escape(source_name)}. Edit freely: the",
        "     project reloads this file live. Check the slots against the paint job",
        "     templates for this vehicle before publishing. -->",
        f'<vehicle id={quoteattr(vid)} kind="{kind}" name={quoteattr(info.name)}'
        + (f' game="{game}"' if game else "")
        + ">",
        '  <!-- <template width="4096" height="4096"/>  set to the template size to get size checks -->',
        '  <slot id="default" main="true"/>',
    ]
    if info.cabins:
        found = ", ".join(
            k + (f" ({v})" if v and not v.startswith("@@") else "") for k, v in sorted(info.cabins.items())
        )
        lines.append(f"  <!-- cabins found: {escape(found)} -->")
    for g in sorted(cab_groups, key=lambda g: (-info.groups[g], g)):
        sid = _slot_name(list(g), used)
        lines.append(f'  <slot id="{sid}">  <!-- used by {info.groups[g]} SCS paint job(s) -->')
        lines += [f"    <acc>{escape(a)}</acc>" for a in g]
        lines.append("  </slot>")
    if other_accs:
        lines.append("  <!-- other accessories SCS paint jobs override (add as slots if you skin them):")
        lines += [f"       {escape(a)}" for a in other_accs[:60]]
        lines.append("  -->")
    if info.problems:
        lines.append(f"  <!-- {info.problems} encrypted/binary SII file(s) could not be read -->")
    lines.append("</vehicle>")
    return "\n".join(lines) + "\n"


def scan(
    source_path: Path, out_dir: Path, game: str = "", overwrite: bool = False, log: Log = print
) -> list[Path]:
    """Write one vehicle XML file per truck / ownable trailer found in ``source_path``.

    Args:
        source_path: A folder containing ``def/vehicle/...`` or a ZIP ``.scs``/``.zip``.
        out_dir: The folder to write ``<vehicle_id>.xml`` files into (created if needed).
        game: ``"ets2"``/``"ats"`` to record in the files, or ``""``.
        overwrite: Replace files that already exist (otherwise they are skipped).
        log: Receives one progress line per vehicle.

    Returns:
        The files written.

    Raises:
        HashFsArchiveError: ``source_path`` is a HashFS archive.
    """
    written: list[Path] = []
    with Source(source_path) as src:
        vehicles: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        for n in src.names:
            m = re.match(r"(?:.*/)?def/vehicle/(truck|trailer_owned)/([^/]+)/", n)
            if m:
                vehicles[(m.group(1), m.group(2))].add(n[: m.end()])
        if not vehicles:
            log("No def/vehicle/truck or def/vehicle/trailer_owned folders found.")
            return written
        out_dir.mkdir(parents=True, exist_ok=True)

        for (kind, vid), bases in sorted(vehicles.items()):
            base = sorted(bases)[0]
            if not all(TOKEN_RE.match(t) for t in vid.split(".")):
                log(f"skip {vid}: not a valid unit id")
                continue
            info = _VehicleScan(vid)
            for f in src.list(base, ".sii"):
                try:
                    text = src.read(f)
                except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                    info.problems += 1
                    continue
                info.add(f[len(base) :], text)

            out = out_dir / f"{vid.replace('.', '_')}.xml"
            if out.exists() and not overwrite:
                log(f"exists, skipped (use --overwrite): {out}")
                continue
            out.write_text(_vehicle_xml(vid, kind, game, source_path.name, info), encoding="utf-8")
            written.append(out)
            log(
                f"{kind:13} {vid:28} cabins: {len(info.cabins):2}  "
                f"cab slots: {len(info.cab_groups())}  -> {out.name}"
            )
    return written


# ==========================================================================
# Vehicle library scan (cabins + paintable accessories), used by the app
# ==========================================================================


class _Layered:
    """Several sources (folder / ZIP / HashFS) read as one; later ones win.

    Each layer is a (list files below a prefix, read a file as text) pair. A game
    folder gives ``def.scs`` followed by its ``dlc_*.scs`` archives. Call :meth:`close`
    when done (or use it as a context manager).
    """

    def __init__(self, path: Path):
        from .hashfs import HashFs

        self.layers: list[tuple[Listing, Reader]] = []
        self._open: list[HashFs | Source] = []
        path = Path(path)
        try:
            if path.is_dir() and (path / "def.scs").is_file():
                for a in [path / "def.scs", *sorted(path.glob("dlc_*.scs"))]:
                    try:
                        self._add_hashfs(HashFs(a))
                    except (OSError, ValueError) as e:  # a damaged or foreign DLC must not stop the scan
                        log.warning("skipping %s: %s", a.name, e)
                        continue
            elif path.is_dir():
                self._add_source(Source(path))
            else:
                with open(path, "rb") as fh:
                    magic = fh.read(4)
                if magic == _HASHFS_MAGIC:
                    self._add_hashfs(HashFs(path))
                else:
                    self._add_source(Source(path))
        except BaseException:
            self.close()
            raise

    def _add_source(self, src: Source) -> None:
        """Add a folder or ZIP layer."""
        self._open.append(src)
        self.layers.append((lambda pre, s=src: s.list(pre), src.read))  # type: ignore[misc]

    def _add_hashfs(self, fs: HashFs) -> None:
        """Add a HashFS archive layer; directory listings are cached per prefix."""
        self._open.append(fs)
        cache: dict[str, list[str]] = {}

        def listing(prefix: str) -> list[str]:
            top = prefix.rstrip("/")
            if top not in cache:
                cache[top] = list(fs.walk(top))
            return cache[top]

        def read(p: str) -> str:
            data = fs.read(p)
            if data is None:
                raise FileNotFoundError(p)
            return _decode_sii(data)

        self.layers.append((listing, read))

    def close(self) -> None:
        """Close every archive."""
        for f in self._open:
            f.close()
        self._open.clear()

    def __enter__(self) -> _Layered:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def files(self, prefix: str) -> dict[str, Reader]:
        """Return {path: reader} of every file below ``prefix``; a later layer's file wins."""
        out: dict[str, Reader] = {}
        for listing, read in self.layers:
            for p in listing(prefix):
                out[p] = read
        return out


def _pretty(token: str) -> str:
    """Turn a token into a label: ``"aero_pt"`` -> ``"Aero Pt"``."""
    return token.replace("_", " ").strip().title()


class _TruckInfo:
    """What :func:`scan_vehicles` learned about one truck."""

    def __init__(self) -> None:
        self.name = ""
        self.cabins: dict[str, str] = {}
        self.acc_names: dict[str, str] = {}
        self.acc_used: Counter[str] = Counter()
        self.models: dict[str, str] = {}  # cabin -> model path (buses use it for window films)
        self.unread = 0

    def add(self, tid: str, rel: str, text: str) -> None:
        """Record the units of one SII file of truck ``tid`` (``rel`` is the path below its folder)."""
        for cls, uname, attrs in units(text):
            parts = uname.split(".")
            nm = (attrs.get("name") or [""])[0]
            if cls == "accessory_truck_data" and nm and not nm.startswith("@@"):
                self.name = nm
            elif cls == "accessory_cabin_data":
                self.cabins[uname] = nm if nm and not nm.startswith("@@") else ""
                model = (attrs.get("model") or [""])[0]
                if model:
                    self.models[uname] = model

            elif cls == "simple_paint_job_data" and rel.startswith("paint_job/"):
                for a in attrs.get("acc_list[]", []):
                    self.acc_used[a] += 1
            # accessory units are named "<item>.<truck id>.<type>"; acc_list values are "<type>.<item>"
            elif cls.startswith("accessory_") and len(parts) >= 3 and ".".join(parts[1:-1]) == tid:
                value = f"{parts[-1]}.{parts[0]}"
                if nm and not nm.startswith("@@"):
                    self.acc_names[value] = nm


def _bus_cabin_name(cab_id: str) -> str:
    """A readable name for a coach body such as ``143d6``: "14.3 m double-deck, 6 wheels".

    The game's own names are translation keys (``@@busbody_dd_40m@@``) that need the language
    files; the cabin id carries the length (decimetres), the deck type and the wheel count.
    """
    m = re.fullmatch(r"(\d{3})([dh])(\d)", cab_id.split(".", maxsplit=1)[0])
    if not m:
        return _pretty(cab_id.split(".", maxsplit=1)[0])
    deck = "double-deck" if m.group(2) == "d" else "high-deck"
    return f"{int(m.group(1)) / 10:.1f} m {deck}, {m.group(3)} wheels"


def _to_vehicle(tid: str, t: _TruckInfo, game: str, source: Path, kind: str = "truck") -> Vehicle:
    """Return the library entry for one scanned truck or coach (``kind`` is ``"truck"`` or ``"bus"``)."""
    from .templates import known_name
    from .vehlib import Accessory, Variant, Vehicle

    brand = tid.split(".", maxsplit=1)[0]
    model = t.name or _pretty(tid.split(".", 1)[-1])
    if kind == "bus":
        full = known_name(tid, game or "ets2")  # "Volvo 9700 Double Decker": drop the brand
        if full:
            model = full.split(" ", 1)[-1] if full.lower().startswith(brand.lower()) else full
    shown_brand = _pretty(brand)
    if kind == "bus":
        full = known_name(tid, game or "ets2")
        if full.lower().startswith(brand.lower()):
            shown_brand = full[: len(brand)]  # "MAN", not "Man"
    v = Vehicle(tid, game, shown_brand, model, "SCS Software", "Paintbooth scan", source=str(source))
    if kind == "bus":
        v.vehicle_type = "Buses"
    for cab_id, nm in sorted(t.cabins.items()):
        name = nm or (_bus_cabin_name(cab_id) if kind == "bus" else _pretty(cab_id.split(".")[0]))
        v.variants.append(Variant(cab_id, name, model=t.models.get(cab_id) if kind == "bus" else None))
    for value in sorted(t.acc_used):
        typ, _, name = value.partition(".")
        label = t.acc_names.get(value) or _pretty(name)
        v.accessories.append(Accessory(value, label, _pretty(typ)))
    return v


def scan_vehicles(source: Path, game: str = "", log: Log = print) -> list[Vehicle]:
    """Return vehicle library entries for every truck with cabins found in ``source``.

    Args:
        source: The game folder (reads ``def.scs`` + ``dlc_*.scs`` directly), one
            HashFS or ZIP ``.scs``, or an extracted folder containing ``def/``.
        game: ``"ets2"``/``"ats"`` to record in the entries.
        log: Receives one summary line per truck.
    """
    with _Layered(Path(source)) as src:
        trucks: dict[tuple[str, str], _TruckInfo] = {}
        files = {**src.files("def/vehicle/truck"), **src.files("def/vehicle/bus")}
        for path, reader in files.items():
            m = re.match(r"def/vehicle/(truck|bus)/([^/]+)/(.+)\.sii$", path)
            if not m:
                continue
            kind, tid, rel = m.groups()
            t = trucks.setdefault((kind, tid), _TruckInfo())
            try:
                text = reader(path)
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                t.unread += 1
                continue
            t.add(tid, rel, text)

    out = []
    for (kind, tid), t in sorted(trucks.items()):
        if not t.cabins:
            continue
        v = _to_vehicle(tid, t, game, source, kind)
        if kind == "bus":
            _add_bus_regions(v, source)
        out.append(v)
        log(
            f"{tid:32} cabins: {len(v.variants):2}  paintable accessories: {len(v.accessories):3}"
            + (f"  ({t.unread} unreadable files)" if t.unread else "")
        )
    return out


def _add_bus_regions(v: Vehicle, source: Path) -> None:
    """Record each bus body's paint tile (``Variant.mask``), read from its model.

    A body whose model can't be read keeps no region; its skins then cover the whole UV space,
    and the problem is logged.
    """
    from .coach import load_cabin, paint_region

    try:
        read = game_reader(source)
    except OSError as e:
        log.warning("no coach models in %s: %s", source, e)
        return
    for x in v.variants:
        if not x.model:
            continue
        try:
            region = paint_region(load_cabin(read, x.model))
        except ValueError as e:
            log.warning("coach body %s: %s", x.id, e)
            continue
        x.mask = region.name if region else None


@functools.lru_cache(maxsize=2)
def game_reader(game_dir: Path) -> Callable[[str], bytes | None]:
    """Return a function that reads any file of the installed game by path, as bytes.

    The DLC archives are searched first (the coaches live in ``dlc_bus.scs``), then the base
    game's. The archives stay open for the life of the process, so repeated exports don't
    re-read their tables; at most two game folders are kept.

    Args:
        game_dir: The game folder (contains ``def.scs``).

    Raises:
        OSError: The folder has no game archives.
    """
    from .hashfs import HashFs

    if game_dir.is_file():  # one archive, e.g. dlc_bus.scs on its own
        names = [game_dir]
    else:
        names = [
            *sorted(game_dir.glob("dlc_*.scs")),
            *(game_dir / n for n in ("base_vehicle.scs", "base_share.scs", "base.scs", "def.scs")),
        ]
    archives = []
    for a in names:
        if not a.is_file():
            continue
        try:
            archives.append(HashFs(a))
        except (OSError, ValueError) as e:  # a damaged or foreign archive must not stop the export
            log.warning("skipping %s: %s", a.name, e)
    if not archives:
        raise OSError(f"no game archives (.scs) in {game_dir}")

    def read(path: str) -> bytes | None:
        for fs in archives:
            data = fs.read(path)
            if data is not None:
                return data
        return None

    return read
