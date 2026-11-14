"""Turn a loaded :class:`~paintbooth.model.Project` into a finished .scs mod - fast.

Two steps:

* :func:`plan` works out every file of the mod (:class:`OutFile`): small text files are
  rendered straight away, textures become :class:`TextureJob` descriptions. The .pb/.ms2
  pipeline (``ms2build`` and ``tplbuild``) produces the same :class:`OutFile` lists.
* :meth:`Builder.build_files` encodes the textures, packs everything and writes the
  .scs/.zip file (or a folder).

Speed comes from three things:

* textures are encoded in parallel (one worker process per CPU core),
* every encoded texture is cached in memory and on disk, keyed by the
  source file's size + modification time + encode settings, so a rebuild
  only re-encodes images that actually changed,
* SII/tobj/mat text is tiny and regenerated every time, so editing XML
  never needs a texture re-encode.

The on-disk cache is pruned to :data:`CACHE_MAX_BYTES` after each build, least recently
used first.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Any

from PIL import Image

from . import __version__
from .formats import dds_bgra_srgb_dx10, dds_bytes, is_dds, sii_file, sii_float3, sii_str, tobj_bytes, ui_mat
from .model import Issue, ModInfo, Project, Skin, Target, Vehicle
from .packer import LEVELS, Member, pack_all, write_zip
from .safepath import UnsafePathError, clean_member, safe_join
from .util import is_pot

log = logging.getLogger(__name__)

ENCODER_VERSION = "3"  # part of every cache key: bump it when encoded bytes change
MOD_ICON_SIZE = (276, 162)
CACHE_MAX_BYTES = 2 * 1024**3  # on-disk texture cache limit (about 100 re-exports of a 4096² skin)
# Marks an output folder as made by Paintbooth, so a rebuild may replace it. The old name is kept
# on purpose: folders built by earlier versions must still be recognised.
BUILD_MARKER = ".skinyard_build"

LogFn = Callable[[str], Any]


@dataclass(frozen=True)
class TextureJob:
    """How to make one texture file of the mod from a source image.

    Attributes:
        src: The source image (or, for "copy", the file to copy unchanged).
        purpose: "mask" (paint job texture, DDS with mips), "icon" (shop icon), "modicon"
            (mod manager JPEG), "film" (bus window/trim film: a mask whose alpha is multiplied
            by ``overlay_alpha``) or "copy" (used as it is).
        size: Resize/crop target; None keeps the source size.
        fmt: DXT5 | DXT1 | BGRA_SRGB | JPG | RAW.
        overlay: Icons only: an image blended on top.
        overlay_alpha: Opacity of ``overlay``; for "film", the factor applied to the alpha.
    """

    src: Path
    purpose: str
    size: tuple[int, int] | None
    fmt: str
    overlay: Path | None = None
    overlay_alpha: float = 0.6

    def key(self) -> str:
        """Cache key: the settings plus each source file's resolved path, size and mtime.

        It stats the files, so it changes when a source is saved. :meth:`Builder.build_files`
        computes it once per build for that reason.

        Raises:
            OSError: A source file is missing.
        """
        parts: list[object] = [ENCODER_VERSION, self.purpose, self.size, self.fmt, self.overlay_alpha]
        for p in (self.src, self.overlay):
            if p is not None:
                st = p.stat()
                parts += [p.resolve(), st.st_size, st.st_mtime_ns]
        return hashlib.sha1("|".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()


@dataclass
class OutFile:
    """One file of the mod: either ready ``data`` or a texture ``job`` to encode.

    Attributes:
        path: Path inside the mod, forward slashes, no leading ``/``. Must not contain ``..``
            or drive letters; :meth:`Builder.build_files` refuses such paths.
        data: The file's bytes, for text and other ready files.
        job: The texture to encode, when ``data`` is None.
    """

    path: str
    data: bytes | None = None
    job: TextureJob | None = None


@dataclass
class BuildResult:
    """What a build did.

    Attributes:
        ok: True if the mod was written.
        output: The written file or folder.
        seconds: Wall-clock time of the build.
        files: Number of files in the mod.
        textures_encoded: Textures encoded in this build.
        textures_cached: Textures taken from the cache.
        size_bytes: Size of the written archive (or the sum of the folder's files).
        issues: Errors and warnings, from loading and from the build.
    """

    ok: bool
    output: Path | None = None
    seconds: float = 0.0
    files: int = 0
    textures_encoded: int = 0
    textures_cached: int = 0
    size_bytes: int = 0
    issues: list[Issue] = field(default_factory=list)


# --------------------------------------------------------------------------
# Image preparation
# --------------------------------------------------------------------------


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale and centre-crop ``img`` to exactly ``size``."""
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    nw, nh = max(tw, round(img.width * scale)), max(th, round(img.height * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def encode(job: TextureJob) -> bytes:
    """Make the bytes of one texture file (runs in a worker process).

    Raises:
        OSError: The source can't be read or is not an image.
    """
    if job.purpose == "copy":
        return job.src.read_bytes()  # a file copied unchanged (e.g. from a Mods Studio 2 template)
    if job.purpose in ("mask", "icon") and job.size is None and job.src.suffix.lower() == ".dds":
        data = job.src.read_bytes()
        if is_dds(data):
            return data  # already a game-ready texture: use as-is
    with Image.open(job.src) as im:
        im.load()
        img = im.convert("RGBA")
    if job.purpose == "film":  # window/trim film (paintbooth.coach): alpha scaled by overlay_alpha
        if job.size and img.size != job.size:
            img = img.resize(job.size, Image.Resampling.LANCZOS)
        a = img.getchannel("A").point(lambda x: round(x * job.overlay_alpha))
        img.putalpha(a)
        return dds_bytes(img, job.fmt, mips=True)
    if job.purpose == "mask":
        if job.size and img.size != job.size:
            img = img.resize(job.size, Image.Resampling.LANCZOS)
        return dds_bytes(img, job.fmt, mips=True)
    if job.size:
        img = _cover(img, job.size)
    if job.overlay is not None:
        with Image.open(job.overlay) as ov_file:
            ov = ov_file.convert("RGBA").resize(img.size, Image.Resampling.LANCZOS)
        a = ov.getchannel("A").point(lambda x: int(x * job.overlay_alpha))
        ov.putalpha(a)
        img = Image.alpha_composite(img, ov)
    if job.fmt == "JPG":
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue()
    return dds_bgra_srgb_dx10(img)


def _worker_init() -> None:
    """Start-up of each encoder process: one thread per process.

    The pool already runs one process per core, so the DXT encoder's own threads (and any
    BLAS threads numpy starts) would only oversubscribe the CPU.
    """
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    from . import bcn

    bcn.THREADS = 1


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def _mask_path(mod: ModInfo, v: Vehicle, skin: Skin, slot: str) -> str:
    """Mod path (without extension) of a skin's mask for one vehicle slot."""
    return f"vehicle/{v.kind}/upgrade/paintjob/{mod.package}/{v.folder}/{skin.id}/{slot}"


@dataclass
class _Plan:
    """Shared state while planning one project."""

    project: Project
    mod: ModInfo
    rel_pfile: str
    files: list[OutFile] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    first_image: Path | None = None
    checked: dict[Path, tuple[int, int]] = field(default_factory=dict)  # image -> size

    def warn(self, line: int, msg: str) -> None:
        """Add a warning about project.xml."""
        self.issues.append(Issue("warning", self.rel_pfile, line, msg))


def _image_size(pl: _Plan, t: Target) -> tuple[int, int] | None:
    """Size of a target's image (read once per image); an unreadable image adds an error."""
    if t.image not in pl.checked:
        try:
            with Image.open(t.image) as im:
                pl.checked[t.image] = im.size
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            pl.issues.append(
                Issue("error", pl.rel_pfile, t.line, f"cannot open image {pl.project.rel(t.image)}: {e}")
            )
            return None
    return pl.checked[t.image]


def _mask_size(pl: _Plan, v: Vehicle, t: Target, size: tuple[int, int]) -> tuple[int, int] | None:
    """The size a mask must be resized to (with a warning), or None to keep it."""
    name = pl.project.rel(t.image)
    if v.template and size != v.template:
        pl.warn(
            t.line,
            f"{name} is {size[0]}x{size[1]}, template for {v.id} is "
            f"{v.template[0]}x{v.template[1]} - it will be resized",
        )
        return v.template
    if not (is_pot(size[0]) and is_pot(size[1])):
        # next power of two, at least 4 (one DXT block)
        pot = (1 << max(2, (size[0] - 1).bit_length()), 1 << max(2, (size[1] - 1).bit_length()))
        pl.warn(t.line, f"{name} is {size[0]}x{size[1]}, not a power of two - resizing to {pot[0]}x{pot[1]}")
        return pot
    return None


def _plan_masks(pl: _Plan, v: Vehicle, skin: Skin, slots: dict[str, Target]) -> None:
    """The .dds/.tobj pair of every slot image of one skin on one vehicle."""
    for slot_id, t in slots.items():
        pl.first_image = pl.first_image or t.image
        size = _image_size(pl, t)
        if size is None:
            continue
        target_size = _mask_size(pl, v, t, size)
        base = _mask_path(pl.mod, v, skin, slot_id)
        pl.files.append(
            OutFile(base + ".dds", job=TextureJob(t.image, "mask", target_size, pl.project.texture_format))
        )
        pl.files.append(OutFile(base + ".tobj", tobj_bytes("/" + base + ".dds")))


def _plan_definitions(pl: _Plan, v: Vehicle, skin: Skin, slots: dict[str, Target], icon_name: str) -> None:
    """The paint job definition of one skin on one vehicle, plus its per-part overrides."""
    attrs: dict[str, str] = {
        "name": sii_str(skin.name),
        "price": str(skin.price),
        "unlock": str(skin.unlock),
        "icon": sii_str(icon_name),
    }
    if skin.mode == "airbrush":
        attrs["airbrush"] = "true"
    for key, rgb in skin.colors.items():
        attrs[key] = sii_float3(rgb)
    if skin.mode != "plain":
        attrs["paint_job_mask"] = sii_str("/" + _mask_path(pl.mod, v, skin, v.main_slot.id) + ".tobj")
    for k, val in skin.raw_attrs:
        attrs[k] = val
    unit = f"{skin.id}.{v.id}.paint_job"
    pl.files.append(
        OutFile(
            f"def/vehicle/{v.kind}/{v.id}/paint_job/{skin.id}.sii",
            sii_file([("accessory_paint_job_data", unit, list(attrs.items()))]).encode(),
        )
    )
    # per-cab / per-part overrides: one anonymous unit per non-main slot
    overrides: list[tuple[str, str, list[tuple[str, str]]]] = []
    for slot_id in slots:
        s = v.slots[slot_id]
        if s.main:
            continue
        kv = [("paint_job_mask", sii_str("/" + _mask_path(pl.mod, v, skin, slot_id) + ".tobj"))]
        kv += [("acc_list[]", sii_str(a)) for a in s.accs]
        overrides.append(("simple_paint_job_data", f".ovr{len(overrides)}", kv))
    if overrides:
        pl.files.append(
            OutFile(
                f"def/vehicle/{v.kind}/{v.id}/paint_job/accessory/{skin.id}.sii",
                sii_file(overrides).encode(),
            )
        )


def _plan_skin(pl: _Plan, skin: Skin) -> None:
    """Every file of one skin: shop icon (dds/tobj/mat), then masks and definitions per vehicle."""
    project = pl.project
    by_vehicle: dict[str, dict[str, Target]] = {}
    for t in skin.targets:
        if t.vehicle in project.vehicles and t.slot in project.vehicles[t.vehicle].slots:
            by_vehicle.setdefault(t.vehicle, {})[t.slot] = t
    if not by_vehicle:
        return

    icon_name = f"{pl.mod.package}_{skin.id}"
    icon_src = skin.icon
    if icon_src is None:
        v0 = project.vehicles[next(iter(by_vehicle))]
        icon_src = by_vehicle[v0.id][v0.main_slot.id].image
    pl.files.append(
        OutFile(
            f"material/ui/accessory/{icon_name}.dds",
            job=TextureJob(icon_src, "icon", project.icon_size, "BGRA_SRGB"),
        )
    )
    pl.files.append(
        OutFile(
            f"material/ui/accessory/{icon_name}.tobj",
            tobj_bytes(f"/material/ui/accessory/{icon_name}.dds", ui=True),
        )
    )
    pl.files.append(OutFile(f"material/ui/accessory/{icon_name}.mat", ui_mat(f"{icon_name}.tobj").encode()))

    for vid, slots in by_vehicle.items():
        v = project.vehicles[vid]
        _plan_masks(pl, v, skin, slots)
        _plan_definitions(pl, v, skin, slots, icon_name)


def _plan_manifest(pl: _Plan) -> None:
    """manifest.sii, the mod manager icon and the description.

    ``compatible_versions`` is written only for versions the project lists (none by default).
    """
    mod = pl.mod
    manifest = [
        ("package_version", sii_str(mod.version)),
        ("display_name", sii_str(mod.title)),
        ("author", sii_str(mod.author)),
    ]
    manifest += [("category[]", sii_str(c)) for c in mod.categories]
    manifest += [("icon", sii_str("mod_icon.jpg")), ("description_file", sii_str("mod_description.txt"))]
    manifest += [("compatible_versions[]", sii_str(c)) for c in mod.compatible]
    pl.files.append(
        OutFile("manifest.sii", sii_file([("mod_package", f".{mod.package}", manifest)]).encode())
    )
    icon_src = mod.icon or pl.first_image
    if icon_src is not None:
        pl.files.append(OutFile("mod_icon.jpg", job=TextureJob(icon_src, "modicon", MOD_ICON_SIZE, "JPG")))
    if mod.description and mod.description.is_file():
        pl.files.append(OutFile("mod_description.txt", mod.description.read_bytes()))
    else:
        pl.files.append(
            OutFile("mod_description.txt", f"{mod.title}\nBuilt with Paintbooth {__version__}.\n".encode())
        )


def _dedupe(pl: _Plan) -> list[OutFile]:
    """Drop repeated identical files; two *different* files for one path are an error.

    Different vehicle ids can share a mask folder (see :attr:`Vehicle.folder`), so this is a real
    check: without it the last file would silently win.
    """
    seen: dict[str, OutFile] = {}
    reported: set[str] = set()
    for f in pl.files:
        old = seen.get(f.path)
        if old is not None and (f.job, f.data) != (old.job, old.data) and f.path not in reported:
            reported.add(f.path)
            pl.issues.append(
                Issue("error", pl.rel_pfile, 0, f"two different files would be written to {f.path}")
            )
        seen[f.path] = f
    return list(seen.values())


def plan(project: Project) -> tuple[list[OutFile], list[Issue]]:
    """Work out every file the mod will contain. Cheap: reads image headers only.

    Args:
        project: A loaded project (normally without errors).

    Returns:
        The files (each path once) and the problems found, such as unreadable images, images
        that will be resized, and output paths that two different files claim.
    """
    rel_pfile = project.rel(project.file)
    if project.mod is None:
        return [], [Issue("error", rel_pfile, 0, "missing <mod> element")]
    pl = _Plan(project, project.mod, rel_pfile)
    for skin in project.skins:
        _plan_skin(pl, skin)
    _plan_manifest(pl)
    return _dedupe(pl), pl.issues


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


class Builder:
    """Encodes, packs and writes mods; keeps a texture cache between builds (used by watch/UI).

    Args:
        cache_dir: Folder of the on-disk texture cache, or None for memory only.
        workers: Encoder processes (and packing threads); default one per CPU core.
        mp_context: The multiprocessing context for the pool (the GUI uses "spawn").
        cache_max_bytes: Size limit of the on-disk cache.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        workers: int | None = None,
        mp_context: BaseContext | None = None,
        cache_max_bytes: int = CACHE_MAX_BYTES,
    ):
        self.cache_dir = cache_dir
        self.mp_context = mp_context
        self.workers = workers or max(1, os.cpu_count() or 1)
        self.cache_max_bytes = cache_max_bytes
        self.memory: dict[str, bytes] = {}  # texture key -> encoded bytes (only keys of the last build)
        self._pool: ProcessPoolExecutor | None = None
        self.packed: dict[tuple[str, int | None], Member] = {}  # (texture key, level) -> packed member

    # ---------------------------------------------------------------- encoding
    def _thread_map(self, jobs: list[TextureJob]) -> list[bytes]:
        """Encode on threads (when processes are not available)."""
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(encode, jobs))

    def _pool_map(self, jobs: list[TextureJob]) -> list[bytes]:
        """Encode ``jobs`` in the process pool, in order.

        Pillow's encoder holds the GIL, so real parallelism needs processes. The pool is kept
        alive between builds so watch mode stays instant. Only a pool that can't start or breaks
        falls back to threads; an error from an image itself is raised at once.
        """
        try:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    self.workers, mp_context=self.mp_context, initializer=_worker_init
                )
            futures: list[Future[bytes]] = [self._pool.submit(encode, j) for j in jobs]
        except (OSError, NotImplementedError, ImportError, RuntimeError) as e:
            # e.g. a frozen build without multiprocessing support
            log.warning("encoder processes unavailable (%s); using threads", e)
            self.close()
            return self._thread_map(jobs)
        try:
            return [f.result() for f in futures]
        except BrokenProcessPool as e:
            log.warning("encoder process pool broke (%s); using threads", e)
            self.close()
            return self._thread_map(jobs)

    def close(self) -> None:
        """Stop the worker processes (they are started again on the next build)."""
        if self._pool is not None:
            self._pool.shutdown(cancel_futures=True)
            self._pool = None

    # ---------------------------------------------------------------- cache
    def _cached(self, key: str) -> bytes | None:
        """Encoded bytes for ``key`` from memory or disk, or None."""
        if key in self.memory:
            return self.memory[key]
        if self.cache_dir:
            p = self.cache_dir / key
            try:
                data = p.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as e:  # locked or unreadable: the texture is simply encoded again
                logging.getLogger(__name__).warning("texture cache not readable (%s): %s", p, e)
                return None
            self.memory[key] = data
            return data
        return None

    def _store(self, key: str, data: bytes) -> None:
        """Keep encoded bytes in memory and (atomically) on disk."""
        self.memory[key] = data
        if self.cache_dir:
            tmp = self.cache_dir / (key + ".tmp")
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, self.cache_dir / key)
            except OSError as e:  # full disk, read-only folder: the export must still succeed
                logging.getLogger(__name__).warning("texture cache not writable (%s): %s", self.cache_dir, e)
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)

    def _touch(self, keys: set[str]) -> None:
        """Mark the cache files of ``keys`` as just used (the pruning order is by mtime)."""
        if not self.cache_dir:
            return
        for k in keys:
            # not on disk (memory only, or removed meanwhile): nothing to keep fresh
            with contextlib.suppress(OSError):
                os.utime(self.cache_dir / k)

    def prune_cache(self, keep: set[str] | frozenset[str] = frozenset()) -> int:
        """Delete the least recently used cache files until the cache fits :attr:`cache_max_bytes`.

        Args:
            keep: Keys that must stay (the textures of the current build).

        Returns:
            The number of bytes freed.
        """
        if not self.cache_dir or not self.cache_dir.is_dir():
            return 0
        entries = []
        for p in self.cache_dir.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_file():
                entries.append((st.st_mtime_ns, st.st_size, p))
        total = sum(e[1] for e in entries)
        freed = 0
        for _mtime, size, p in sorted(entries):
            if total <= self.cache_max_bytes:
                break
            if p.name in keep:
                continue
            try:
                p.unlink()
            except OSError as e:
                log.warning("cannot remove cache file %s: %s", p, e)
                continue
            total -= size
            freed += size
        return freed

    def clear_cache(self) -> None:
        """Forget every cached texture, in memory and on disk."""
        self.memory.clear()
        self.packed.clear()
        if self.cache_dir and self.cache_dir.is_dir():
            shutil.rmtree(self.cache_dir, ignore_errors=True)

    # ---------------------------------------------------------------- building
    def build(self, project: Project, output: Path | None = None, log: LogFn = print) -> BuildResult:
        """Plan and build a ``project.xml`` project.

        Args:
            project: The loaded project.
            output: Where to write; default the project's ``<build output>``.
            log: Receives progress lines.

        Returns:
            The result; nothing is written when the project or its images have errors.
        """
        t0 = time.perf_counter()
        res = BuildResult(ok=False, issues=list(project.issues))
        if project.errors or project.mod is None:
            log(f"Not building: {len(project.errors)} error(s) in the project files.")
            res.seconds = time.perf_counter() - t0
            return res
        files, plan_issues = plan(project)
        res.issues += plan_issues
        if any(i.level == "error" for i in plan_issues):
            log("Not building: errors found while checking images.")
            res.seconds = time.perf_counter() - t0
            return res
        out = output or project.output or project.root / "build" / f"{project.mod.package}.scs"
        return self.build_files(
            files,
            Path(out),
            project.compression,
            project.root,
            project.rel(project.file),
            res,
            log,
            t0,
        )

    def build_files(
        self,
        files: list[OutFile],
        out: Path,
        compression: str,
        root: Path,
        issue_file: str = "",
        res: BuildResult | None = None,
        log: LogFn = print,
        t0: float | None = None,
    ) -> BuildResult:
        """Encode (with cache), pack and write ``files`` to ``out``.

        Args:
            files: The mod's files; each path should appear once.
            out: A .scs/.zip file (written atomically), or a folder. A folder is only replaced if
                it is empty or a previous Paintbooth build.
            compression: "store", "fast" or "small" (archives only).
            root: The project folder; only used to shorten paths in messages and to refuse
                replacing the project folder itself.
            issue_file: The file name the build's issues are reported against.
            res: A result to add to (``build`` passes the loading issues in).
            log: Receives progress lines.
            t0: Start time for :attr:`BuildResult.seconds`; default now.

        Returns:
            The result. Problems (unsafe paths, unreadable images, a locked output) are reported
            as issues with ``ok=False``; nothing is raised for them.
        """
        t0 = t0 or time.perf_counter()
        res = res or BuildResult(ok=False)

        def fail(msg: str, log_line: str) -> BuildResult:
            res.issues.append(Issue("error", issue_file, 0, msg))
            log(log_line)
            res.seconds = time.perf_counter() - t0
            return res

        bad = _unsafe_paths(files)
        if bad:
            return fail(f"unsafe file path in the mod: {bad[0]!r}", f"Not built: unsafe file path {bad[0]!r}")
        # one key per texture, computed once: a source saved during the build must not change it
        try:
            keys = {f.job: f.job.key() for f in files if f.job is not None}
        except OSError as e:
            return fail(f"could not read an image: {e}", f"Not built: could not read an image ({e})")
        results = self._encode_all(keys, res, log)
        if isinstance(results, str):
            return fail(
                f"could not convert an image: {results}", f"Not built: could not convert an image ({results})"
            )

        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        files = sorted(files, key=lambda f: f.path)
        payload = [(f.path, results[keys[f.job]] if f.job is not None else f.data or b"") for f in files]
        if out.suffix.lower() in (".scs", ".zip"):
            err = self._write_archive(files, payload, keys, out, compression)
        else:
            err = _write_folder(out, root, payload)
        if err is not None:
            return fail(*err)
        res.size_bytes = (
            out.stat().st_size if out.suffix.lower() in (".scs", ".zip") else sum(len(d) for _, d in payload)
        )
        self.prune_cache(set(keys.values()))
        res.ok, res.output, res.files = True, out, len(payload)
        res.seconds = time.perf_counter() - t0
        log(
            f"Built {_rel(out, root)}: {res.files} files, {res.size_bytes / 1e6:.1f} MB, "
            f"{res.textures_encoded} encoded / {res.textures_cached} cached, {res.seconds:.2f}s"
        )
        return res

    def _encode_all(
        self, keys: dict[TextureJob, str], res: BuildResult, log: LogFn
    ) -> dict[str, bytes] | str:
        """Texture key -> bytes for every job: cache hits first, then the rest in parallel.

        Sets the encoded/cached counts of ``res``. Returns the error text instead when an image
        can't be encoded.
        """
        jobs: dict[str, TextureJob] = {}
        results: dict[str, bytes] = {}
        for job, k in keys.items():
            hit = self._cached(k)
            if hit is not None:
                results[k] = hit
            elif k not in jobs:
                jobs[k] = job
        res.textures_cached = len(set(keys.values())) - len(jobs)
        self._touch(set(results))
        if jobs:
            n = min(self.workers, len(jobs))
            log(f"Encoding {len(jobs)} texture(s) on {n} core(s)...")
            todo = list(jobs.values())
            try:
                encoded = [encode(j) for j in todo] if n == 1 else self._pool_map(todo)
            except Exception as e:  # a broken image of any kind is logged and reported, not raised
                logging.getLogger(__name__).warning("texture encoding failed", exc_info=True)
                return str(e)
            for k, data in zip(jobs, encoded, strict=True):
                self._store(k, data)
                results[k] = data
        res.textures_encoded = len(jobs)
        used = set(keys.values())
        for k in list(self.memory):
            if k not in used:
                del self.memory[k]
        return results

    def _write_archive(
        self,
        files: list[OutFile],
        payload: list[tuple[str, bytes]],
        keys: dict[TextureJob, str],
        out: Path,
        compression: str,
    ) -> tuple[str, str] | None:
        """Pack ``payload`` and write it as a ZIP; returns (issue, log line) on failure.

        Compressed texture members are cached too, so a rebuild after an XML-only change just
        copies bytes into the archive.
        """
        level = LEVELS[compression]
        members: list[Member | None] = []
        todo = []
        for i, f in enumerate(files):
            m = self.packed.get((keys[f.job], level)) if f.job is not None else None
            members.append(m)
            if m is None:
                todo.append(i)
        for i, m in zip(todo, pack_all([payload[i][1] for i in todo], level, self.workers), strict=True):
            members[i] = m
            job = files[i].job
            if job is not None:
                self.packed[(keys[job], level)] = m
        live = {(keys[f.job], level) for f in files if f.job is not None}
        for k in list(self.packed):
            if k not in live:
                del self.packed[k]
        entries = [(f.path, m) for f, m in zip(files, members, strict=True) if m is not None]
        try:
            write_zip(out, entries)
        except PermissionError:
            return (
                f"cannot replace {out.name}: it is in use (close the game or Mod Manager and build again)",
                f"Not built: {out.name} is locked by another program (is the game running?)",
            )
        return None


def _rel(p: Path, root: Path) -> str:
    """``p`` relative to ``root`` for messages, or as given if outside it."""
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def _unsafe_paths(files: list[OutFile]) -> list[str]:
    """The mod paths that are absolute or contain ``..`` / drive letters (a zip-slip in the mod)."""
    bad = []
    for f in files:
        try:
            clean_member(f.path)
        except UnsafePathError:
            bad.append(f.path)
    return bad


def _write_folder(out: Path, root: Path, payload: list[tuple[str, bytes]]) -> tuple[str, str] | None:
    """Write the mod as a folder; returns (issue, log line) on failure.

    Only an empty folder or a previous Paintbooth build (it has :data:`BUILD_MARKER`) is
    replaced, and never the project folder or one of its parents.
    """
    if out.exists():
        rootr = Path(root).resolve()
        o = out.resolve()
        marker = o / BUILD_MARKER
        if o == rootr or o in rootr.parents or (any(o.iterdir()) and not marker.exists()):
            return (
                f"refusing to overwrite {out}: not empty and not a previous Paintbooth build",
                f"Not building: output folder {out} is not empty and was not made by Paintbooth.",
            )
        shutil.rmtree(o)
    out.mkdir(parents=True)
    (out / BUILD_MARKER).write_text("Created by Paintbooth. This folder is replaced on every build.\n")
    for path, data in payload:
        p = safe_join(out, path)  # paths were checked already; this also refuses symlinked escapes
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return None
