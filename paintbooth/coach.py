"""Coaches (the ETS2 "Coaches" DLC): UV templates, and painted windows and gloss-black trim.

Buses are ordinary paint job vehicles in ``def/vehicle/bus/...``, so skins for them are built by
:mod:`paintbooth.ms2build` like truck skins. This module adds what is new for buses:

**Paint job mask regions.** The coaches' paint UV space is a virtual 4096x4096 texture made of
4096x2048 tiles named after their place in it: ``pjm_at_0x2048_size_4096x2048.tobj`` is the
lower half. A paint job names one tile and the game loads the other tiles of the same folder
with it, so the name SCS's paint jobs use does not say which half a body paints. Paintbooth
reads that from the model instead (:func:`paint_region`): the half its paint UVs lie in. A bus
skin image is that half only (4096x2048), and the exported texture is named after it.

**UV templates.** :func:`body_template` draws every paintable triangle of a cabin in its region,
so the user can paint over it.

**Windows and gloss-black trim.** These parts don't take a paint job in the game. When a skin
asks for it, the export adds *films*: copies of the glass and dark glossy pieces, lifted 1.5 mm
off the surface, with a material that shows the skin's "windows and trim" image. That image has
its own layout, made by :func:`film_template`: flat views of the coach from the left, right,
front, rear and top, each seen from outside, so a window is painted where it is seen. Pixels
left transparent keep the stock look. Window films let half the light through (the image's
alpha is halved) and exist on both faces, so passengers see the same artwork from inside;
trim films are opaque where painted.

The films change the cabin's model files (``.pmd`` + ``.pmg``), which the mod then overrides:
while the mod is active, that cabin shows the films whatever paint job is bought. The model
files are made from the user's own game files at export time, so a game update that changes
the coach needs a new export.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import pmd, pmg

#: The virtual paint UV space of the coaches, in pixels.
VIRTUAL = 4096
#: Film template size (pixels) and how far films sit off the surface (metres).
FILM_SIZE = (4096, 2048)
FILM_LIFT = 0.0015
#: Window films show the artwork at this opacity.
GLASS_OPACITY = 0.5

_REGION_RE = re.compile(r"pjm_at_(\d+)x(\d+)_size_(\d+)x(\d+)")

Reader = Callable[[str], bytes | None]


@dataclass(frozen=True)
class Region:
    """The part of the virtual paint UV space a cabin uses, in pixels."""

    x: int
    y: int
    w: int
    h: int

    @property
    def name(self) -> str:
        """The mask file name SCS uses for this region (without extension)."""
        return f"pjm_at_{self.x}x{self.y}_size_{self.w}x{self.h}"

    @classmethod
    def parse(cls, text: str) -> Region | None:
        """Read a region from a mask path or name such as ``pjm_at_0x2048_size_4096x2048``."""
        m = _REGION_RE.search(text or "")
        return cls(*map(int, m.groups())) if m else None


#: The tiles the coaches' paint UV space is made of (see the module docstring).
TILES = (Region(0, 0, 4096, 2048), Region(0, 2048, 4096, 2048))


# ---------------------------------------------------------------------------------- materials
@dataclass
class _Mat:
    """What the film logic needs to know about one material of the model."""

    effect: str
    diffuse: float  # brightest diffuse channel
    shininess: float
    tint: tuple[float, float, float] | None  # glass only


def _parse_mat(text: str) -> _Mat:
    """Read the effect name and a few values from a ``.mat`` file's text."""

    def num(key: str, default: float) -> float:
        m = re.search(rf"\b{key}\s*:\s*([-\d.]+)", text)
        return float(m.group(1)) if m else default

    def vec(key: str) -> tuple[float, ...] | None:
        m = re.search(rf"\b{key}\s*:\s*\{{([^}}]+)\}}", text)
        return tuple(float(x) for x in m.group(1).split(",")) if m else None

    eff = re.search(r'effect\s*:\s*"([^"]+)"', text)
    tint = vec("glass_tint_color")
    return _Mat(
        effect=eff.group(1) if eff else "",
        diffuse=max(vec("diffuse") or (1.0,)),
        shininess=num("shininess", 0.0),
        tint=(tint[0], tint[1], tint[2]) if tint and len(tint) >= 3 else None,
    )


def is_paint(m: _Mat) -> bool:
    """True for the body paint material (the one the paint job colours)."""
    return "truckpaint" in m.effect


def is_window(m: _Mat) -> bool:
    """True for clear or lightly tinted glass; coloured lamp lenses are left out."""
    return "glass" in m.effect and (m.tint is None or min(m.tint) >= 0.4)


def is_gloss_black(m: _Mat) -> bool:
    """True for dark, glossy, untextured-looking parts (window surrounds, pillars, trims)."""
    skip = ("truckpaint", "glass", "shadow", "lamp", "decal", ".a.", "flare")
    return not any(k in m.effect for k in skip) and m.diffuse < 0.05 and m.shininess >= 100


# ---------------------------------------------------------------------------------- models
@dataclass
class CabinModel:
    """A cabin's model, loaded from the game, with its materials classified."""

    pmd_path: str
    pmd_data: bytes
    model: pmg.Model
    mats: list[_Mat]

    def pieces(self, test: Callable[[_Mat], bool]) -> list[pmg.Piece]:
        """The pieces whose material passes ``test``."""
        return [p for p in self.model.pieces if test(self.mats[p.material])]


def load_cabin(read: Reader, pmd_path: str) -> CabinModel:
    """Load a cabin model (``.pmd`` + ``.pmg``) and its first look's materials from the game.

    Args:
        read: Returns a game file's bytes by path (no leading ``/``), or None.
        pmd_path: The cabin's ``model`` from its definition, e.g. ``/vehicle/bus/x/cab.pmd``.

    Raises:
        ValueError: A file is missing or damaged (:class:`pmg.PmgError` / :class:`pmd.PmdError`).
    """
    rel = pmd_path.lstrip("/")
    d = read(rel)
    g = read(rel[: -len(".pmd")] + ".pmg")
    if d is None or g is None:
        raise ValueError(f"model {pmd_path} not found in the game files")
    mats = []
    for path in pmd.material_table(d)[0]:
        text = read(path.lstrip("/"))
        mats.append(_parse_mat(text.decode("utf-8", "replace") if text else ""))
    return CabinModel(pmd_path, d, pmg.read(g), mats)


def paint_region(cab: CabinModel) -> Region | None:
    """Return the tile of the paint UV space the cabin's paint pieces use, or None.

    Paint pieces carry several UV sets (detail, normal map, paint job); the paint job set is the
    one that fits inside one tile.
    """
    for p in cab.pieces(is_paint):
        for uv in p.uvs:
            for tile in TILES:
                if _inside(uv, tile):
                    return tile
    return None


def _inside(uv: np.ndarray, region: Region) -> bool:
    """True if every UV (0-1 of the virtual space) lies in ``region``, with a 1 % margin."""
    lo = np.array([region.x, region.y]) / VIRTUAL
    hi = np.array([region.x + region.w, region.y + region.h]) / VIRTUAL
    return bool(len(uv)) and bool((uv.min(axis=0) >= lo - 0.01).all() and (uv.max(axis=0) <= hi + 0.01).all())


def paint_uvs(cab: CabinModel, region: Region) -> list[tuple[pmg.Piece, np.ndarray]]:
    """Return each paint piece with its paint job UV set, in region pixels.

    The paint job UV set is the one that lies inside the cabin's region of the virtual space
    (the others are normal-map or detail sets).
    """
    out = []
    for p in cab.pieces(is_paint):
        for uv in p.uvs:
            if _inside(uv, region):
                px = uv * VIRTUAL - [region.x, region.y]
                out.append((p, px))
                break
    return out


# ---------------------------------------------------------------------------------- film layout
# The five views of the film template. Each maps a face pointing along `normal` to the image,
# seen from outside: (normal axis, sign, u axis, u sign, v axis, v sign). v grows downwards.
_VIEWS = {
    "left": (0, -1, 2, +1, 1, -1),
    "right": (0, +1, 2, -1, 1, -1),
    "front": (2, -1, 0, -1, 1, -1),
    "rear": (2, +1, 0, +1, 1, -1),
    "top": (1, +1, 2, +1, 0, -1),  # seen from above, front on the left, right side up
}


@dataclass
class FilmLayout:
    """Where each view of the coach sits in the film template (all in pixels)."""

    scale: float  # pixels per metre
    origin: dict[str, tuple[float, float]]  # view -> pixel of the view's minimum (u, v) corner
    lo: np.ndarray  # model bounding box
    hi: np.ndarray

    @classmethod
    def for_model(cls, model: pmg.Model) -> FilmLayout:
        """Lay out the five views for this model's size.

        Rows: left side + front, right side + rear, top. A 40 px gap separates the views.
        """
        allpos = np.concatenate([p.positions for p in model.pieces if len(p.positions)])
        lo, hi = allpos.min(axis=0), allpos.max(axis=0)
        size = hi - lo  # x width, y height, z length
        gap = 40
        w_m, h_m = size[2] + size[0], 2 * size[1] + size[0]
        scale = min((FILM_SIZE[0] - 3 * gap) / w_m, (FILM_SIZE[1] - 4 * gap) / h_m)
        row = [gap, 2 * gap + size[1] * scale, 3 * gap + 2 * size[1] * scale]
        col2 = 2 * gap + size[2] * scale
        origin = {
            "left": (gap, row[0]),
            "front": (col2, row[0]),
            "right": (gap, row[1]),
            "rear": (col2, row[1]),
            "top": (gap, row[2]),
        }
        return cls(scale, origin, lo, hi)

    def project(self, view: str, pos: np.ndarray) -> np.ndarray:
        """Return the template pixel (u, v) of model-space points ``pos`` in ``view``."""
        _, _, ua, us, va, vs = _VIEWS[view]
        # measure from the bounding box edge that is at the view's top-left
        u0 = self.lo[ua] if us > 0 else self.hi[ua]
        v0 = self.lo[va] if vs > 0 else self.hi[va]
        u = (pos[:, ua] - u0) * us * self.scale
        v = (pos[:, va] - v0) * vs * self.scale
        ox, oy = self.origin[view]
        return np.c_[u + ox, v + oy]


def _face_views(p: pmg.Piece) -> list[str]:
    """The view each triangle of ``p`` belongs to, from its face normal (bottom faces go to top)."""
    a, b, c = (p.positions[p.triangles[:, k]] for k in range(3))
    n = np.cross(b - a, c - a)
    # the stored winding may be either way; trust the vertex normals' direction
    vn = p.normals[p.triangles].sum(axis=1)
    n *= np.sign((n * vn).sum(axis=1, keepdims=True) + 1e-12)
    axis = np.abs(n).argmax(axis=1)
    sign = np.sign(n[np.arange(len(n)), axis])
    names = []
    for ax, sg in zip(axis, sign, strict=True):
        if ax == 1:
            names.append("top")
        else:
            names.append(next(v for v, (na, ns, *_) in _VIEWS.items() if na == ax and ns == sg))
    return names


def film_piece(src: pmg.Piece, layout: FilmLayout, material: int, double_sided: bool) -> list[pmg.Piece]:
    """Make the film pieces over ``src``: its triangles, lifted off the surface, with film UVs.

    Every triangle gets its own three vertices, so the UVs of neighbouring triangles seen in
    different views don't have to agree. With ``double_sided`` a reversed copy, lifted the other
    way, is added so the film is seen from inside too. Pieces are split to stay under 65535
    vertices.
    """
    views = _face_views(src)
    tri = src.triangles
    pos = src.positions[tri].reshape(-1, 3)
    nrm = src.normals[tri].reshape(-1, 3)
    rgba = src.rgba[tri].reshape(-1) if src.rgba is not None else None
    uv = np.zeros((len(pos), 2))
    flat_views = np.repeat(np.array(views), 3)
    for v in _VIEWS:
        sel = flat_views == v
        if sel.any():
            uv[sel] = layout.project(v, pos[sel])
    uv /= np.array(FILM_SIZE, dtype=float)

    def build(sign: float) -> tuple[np.ndarray, ...]:
        p = pos + nrm * FILM_LIFT * sign
        n = nrm * sign
        t = np.arange(len(pos)).reshape(-1, 3)
        if sign < 0:
            t = t[:, ::-1]  # reversed winding faces inwards
        return p, n, t

    parts = [build(1.0)] + ([build(-1.0)] if double_sided else [])
    out = []
    per = (0xFFFF // 3) * 3  # vertices per output piece, whole triangles only
    for p, n, t in parts:
        for start in range(0, len(p), per):
            stop = min(start + per, len(p))
            sl = slice(start, stop)
            out.append(
                pmg.Piece(
                    material,
                    p[sl].astype("f4"),
                    n[sl].astype("f4"),
                    [uv[sl].astype("f4")],
                    rgba[sl] if rgba is not None else None,
                    t[start // 3 : stop // 3] - start,
                )
            )
    return out


# ---------------------------------------------------------------------------------- templates
def body_template(cab: CabinModel, region: Region) -> Image.Image:
    """Draw the cabin's paintable surface in its mask region: grey islands, light outlines."""
    img = Image.new("RGBA", (region.w, region.h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for p, px in paint_uvs(cab, region):
        for t in p.triangles:
            d.polygon([tuple(px[k]) for k in t], fill=(128, 128, 128, 255))
    for p, px in paint_uvs(cab, region):
        for t in p.triangles:
            d.polygon([tuple(px[k]) for k in t], outline=(235, 235, 235, 255))
    return img


def film_template(cab: CabinModel) -> Image.Image:
    """Draw the windows-and-trim template: windows blue, gloss-black parts yellow, body grey.

    The body outline (from the paint pieces) is only there to find one's way around; painting
    it has no effect. Everything not drawn is transparent.
    """
    layout = FilmLayout.for_model(cab.model)
    img = Image.new("RGBA", FILM_SIZE, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    layers = [(is_paint, None, (90, 90, 90, 255)), (is_gloss_black, (60, 50, 0, 255), (255, 200, 0, 255))]
    layers.append((is_window, (20, 50, 90, 255), (90, 170, 255, 255)))
    for test, fill, line in layers:
        for p in cab.pieces(test):
            for piece in film_piece(p, layout, 0, double_sided=False):
                px = piece.uvs[0] * FILM_SIZE
                for t in piece.triangles:
                    d.polygon([tuple(px[k]) for k in t], fill=fill, outline=line)
    for view, (ox, oy) in layout.origin.items():
        d.text((ox, oy - 30), view.upper(), fill=(255, 255, 255, 255))
    return img


# ---------------------------------------------------------------------------------- export
FILM_MAT = """effect : "eut2.dif.spec.weight.a.rfx" {{
\tadditional_ambient : 0.000000
\tdiffuse : {{ 1.000000 , 1.000000 , 1.000000 }}
\treflection : 0.000000
\tshininess : {shininess:.6f}
\tspecular : {{ {spec:.6f} , {spec:.6f} , {spec:.6f} }}
\ttexture : "texture_base" {{
\t\tsource : "{tobj}"
\t\tsampler : default
\t}}
}}
"""


def film_material(tobj: str, glossy: bool) -> bytes:
    """The ``.mat`` text of a film: alpha-blended, showing ``tobj``."""
    shininess, spec = (250.0, 1.0) if glossy else (95.0, 0.3)
    return FILM_MAT.format(shininess=shininess, spec=spec, tobj=tobj).encode()


def with_films(cab: CabinModel, glass_mat: str, trim_mat: str) -> tuple[bytes, bytes]:
    """Return the cabin's ``.pmd`` and ``.pmg`` with window and trim films added.

    Args:
        cab: The cabin model from the game.
        glass_mat: Mod path of the window film material (``/...mat``).
        trim_mat: Mod path of the trim film material.

    Returns:
        ``(pmd bytes, pmg bytes)``. The original pieces and materials are unchanged.
    """
    looks = pmd.material_table(cab.pmd_data)
    n = len(looks[0])
    layout = FilmLayout.for_model(cab.model)
    new: list[pmg.Piece] = []
    for p in cab.pieces(is_window):
        new += film_piece(p, layout, n, double_sided=True)
    for p in cab.pieces(is_gloss_black):
        new += film_piece(p, layout, n + 1, double_sided=False)
    pmd_bytes = pmd.add_materials(cab.pmd_data, [glass_mat, trim_mat])
    return pmd_bytes, pmg.add_pieces(cab.model, new)


def write_templates(read: Reader, vehicles: Iterable[object], out_dir: Path) -> list[Path]:
    """Write the UV templates of every bus body into ``out_dir``.

    For each body two PNGs go into ``<out_dir>/<Brand Model>/``: ``<body> paint.png`` (the paint
    job template, its region of the UV space) and ``<body> windows and trim.png`` (the film
    template). Bodies without a model or region are skipped.

    Args:
        read: Reads a game file by path (see :func:`paintbooth.scanner.game_reader`).
        vehicles: Library entries; only buses are used.
        out_dir: Usually ``Templates/ETS2/Buses``.

    Returns:
        The files written.
    """
    from .templates import safe_name

    written: list[Path] = []
    for v in vehicles:
        if not getattr(v, "is_bus", False):
            continue
        folder = out_dir / safe_name(f"{v.brand} {v.name}")  # type: ignore[attr-defined]
        for x in v.variants:  # type: ignore[attr-defined]
            region = Region.parse(x.mask or "")
            if not (x.model and region):
                continue
            cab = load_cabin(read, x.model)
            folder.mkdir(parents=True, exist_ok=True)
            label = safe_name(x.name)
            for suffix, img in (
                ("paint", body_template(cab, region)),
                ("windows and trim", film_template(cab)),
            ):
                path = folder / f"{label} {suffix}.png"
                img.save(path)
                written.append(path)
    return written
