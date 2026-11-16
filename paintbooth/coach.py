"""Coaches (the ETS2 "Coaches" DLC): the blueprint template, baking, and painted windows and trim.

Buses are ordinary paint job vehicles in ``def/vehicle/bus/...``, so skins for them are built by
:mod:`paintbooth.ms2build` like truck skins. This module adds what is new for buses:

**Paint job mask regions.** The coaches' paint UV space is a virtual 4096x4096 texture made of
4096x2048 tiles named after their place in it: ``pjm_at_0x2048_size_4096x2048.tobj`` is the
lower half. A paint job names one tile and the game loads the other tiles of the same folder
with it, so the name SCS's paint jobs use does not say which half a body paints. Paintbooth
reads that from the model instead (:func:`paint_region`): the half its paint UVs lie in. A bus
skin image is that half only (4096x2048), and the exported texture is named after it.

**One blueprint per body.** The user paints a single image, the *blueprint*
(:func:`blueprint_template`): flat views of the coach from the left, right, front, rear and top,
each seen from outside, with the body, the default accessories (bumpers, panels...), the windows
and the gloss-black trim drawn in. At export :func:`bake` turns it into the paint job texture:
every texel of the game's UV layout is traced back to its point on the coach, and from there to
the blueprint. So one stripe painted across the blueprint runs across the paint, the bumpers and,
with the option below, the windows, without lining up three layouts by hand. Painting straight
on the game's layout (:func:`uv_template`) still works for those who prefer it.

**Windows and gloss-black trim.** These parts don't take a paint job in the game. When a skin
asks for it, the export adds *films*: copies of the glass and dark glossy pieces, lifted 1.5 mm
off the surface, whose UVs are their positions in the blueprint, so they show the same image.
Pixels left transparent keep the stock look. Window films let half the light through (the
image's alpha is halved) and exist on both faces, so passengers see the same artwork from
inside; trim films are opaque where painted.

The films change the cabin's model files (``.pmd`` + ``.pmg``), which the mod then overrides:
while the mod is active, that cabin shows the films whatever paint job is bought. The model
files are made from the user's own game files at export time, so a game update that changes
the coach needs a new export.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import pmd, pmg

#: Raise when the templates change, so start-up writes them again for existing users.
TEMPLATE_VERSION = 5
#: The virtual paint UV space of the coaches, in pixels.
VIRTUAL = 4096
#: Film template size (pixels) and how far films sit off the surface (metres).
BLUEPRINT_SIZE = (4096, 2048)
FILM_SIZE = BLUEPRINT_SIZE  # 0.12.0 name
FILM_LIFT = 0.003
#: Vertex colour of the films: about 1.0 in SCS's doubled vertex colours, like the body paint.
FILM_RGBA = 0xFF7D7D7D
#: Seen from inside the coach, window films show the artwork at this opacity (outside: fully).
GLASS_OPACITY = 0.5

_REGION_RE = re.compile(r"pjm_at_(\d+)x(\d+)_size_(\d+)x(\d+)")

Reader = Callable[[str], bytes | None]
log = logging.getLogger(__name__)


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
    texture: str = ""  # the texture_base file name, lower case ("" when there is none)


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
    src = re.search(r'texture\s*:\s*"texture_base"\s*\{[^}]*?source\s*:\s*"([^"]+)"', text)
    return _Mat(
        effect=eff.group(1) if eff else "",
        diffuse=max(vec("diffuse") or (1.0,)),
        shininess=num("shininess", 0.0),
        tint=(tint[0], tint[1], tint[2]) if tint and len(tint) >= 3 else None,
        texture=src.group(1).rsplit("/", 1)[-1].lower() if src else "",
    )


def is_paint(m: _Mat) -> bool:
    """True for the body paint material (the one the paint job colours)."""
    return "truckpaint" in m.effect


def is_window(m: _Mat) -> bool:
    """True for clear or lightly tinted glass; coloured lamp lenses are left out."""
    return "glass" in m.effect and (m.tint is None or min(m.tint) >= 0.4)


def is_gloss_black(m: _Mat) -> bool:
    """True for gloss-black parts (window surrounds, pillars, grilles).

    SCS makes them two ways: a dark diffuse colour on a glossy material (Volvo), or a black
    texture such as ``maintruckpaint_black`` (MAN). Metal (chrome, aluminium) is never trim, even
    when its diffuse colour is dark.
    """
    skip = ("truckpaint", "glass", "shadow", "lamp", "decal", ".a.", "flare")
    if any(k in m.effect for k in skip) or any(k in m.texture for k in ("chrome", "alu", "metal", "steel")):
        return False
    glossy = m.shininess >= 100
    return glossy and (m.diffuse < 0.05 or "black" in m.texture)


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


# ---------------------------------------------------------------------------------- the body
@dataclass
class Body:
    """A coach body: its cabin model plus the accessory models drawn on and painted from the blueprint.

    Attributes:
        cab: The cabin model.
        defaults: Accessories the body comes with (bumpers, panels, window frames...).
        others: Other accessories for the body that take the paint job (alternative bumpers...);
            they are painted from the blueprint too, but not drawn on it.
    """

    cab: CabinModel
    defaults: list[CabinModel]
    others: list[CabinModel]
    #: The definition (``.sii`` path) and model of each default accessory, by accessory type.
    default_defs: dict[str, tuple[str, str]] = field(default_factory=dict)

    def all_models(self) -> list[CabinModel]:
        """Every model, cabin first."""
        return [self.cab, *self.defaults, *self.others]

    def pieces(self, test: Callable[[_Mat], bool], others: bool = False) -> list[pmg.Piece]:
        """The pieces passing ``test`` of the cabin and its default accessories (and the others)."""
        models = [self.cab, *self.defaults, *(self.others if others else [])]
        return [p for m in models for p in m.pieces(test)]


_DEFAULTS_RE = re.compile(r'defaults\[\]\s*:\s*"([^"]+)"')
_EXTERIOR_RE = re.compile(r'exterior_model\s*:\s*"([^"]+)"')
_SUITABLE_RE = re.compile(r'suitable_for\[\]\s*:\s*"([^"]+)"')


def _accessory_model(read: Reader, sii_path: str) -> str | None:
    """The ``exterior_model`` of an accessory definition, or None."""
    data = read(sii_path.lstrip("/"))
    m = _EXTERIOR_RE.search(data.decode("utf-8", "replace")) if data else None
    return m.group(1) if m else None


def load_body(read: Reader, vehicle_id: str, cabin_id: str, model: str) -> Body:
    """Load a coach body with its accessories from the game.

    The cabin definition (``def/vehicle/bus/<vehicle>/cabin/<cabin>.sii``) lists the default
    accessories. When ``read`` can list folders (``read.listdir``), the other accessories of
    the same types that fit this cabin and take paint are loaded too. Accessory models without a
    accessory model is moved from its own coordinates onto the cabin locator named after its
    type (``f_bumper``, ``r_panel``...), as the game does.

    Raises:
        ValueError: The cabin model is missing or damaged. Unreadable accessories are skipped.
    """
    cab = load_cabin(read, model)
    short = cabin_id.split(".", 1)[0]
    defaults: list[str] = []
    kinds: dict[str, str] = {}  # model path -> accessory type (= the cabin locator it hangs on)
    default_defs: dict[str, tuple[str, str]] = {}
    types: set[str] = set()
    # defaults come from the cabin (Volvo) and from the vehicle's data.sii (MAN, Scania)
    for def_file in (
        f"def/vehicle/bus/{vehicle_id}/cabin/{short}.sii",
        f"def/vehicle/bus/{vehicle_id}/data.sii",
    ):
        text = (read(def_file) or b"").decode("utf-8", "replace")
        for d in _DEFAULTS_RE.findall(text):
            if "/accessory/" not in d:
                continue
            acc_text = (read(d.lstrip("/")) or b"").decode("utf-8", "replace")
            fits = _SUITABLE_RE.findall(acc_text)
            m = _EXTERIOR_RE.search(acc_text)
            if m and (not fits or cabin_id in fits) and m.group(1) not in defaults:
                kind = d.split("/accessory/", 1)[1].split("/", 1)[0]
                types.add(kind)
                kinds.setdefault(m.group(1), kind)
                default_defs.setdefault(kind, (d, m.group(1)))
                defaults.append(m.group(1))
    others: list[str] = []
    listdir: Callable[[str], list[str]] | None = getattr(read, "listdir", None)
    for t in sorted(types):
        folder = f"def/vehicle/bus/{vehicle_id}/accessory/{t}"
        for name in listdir(folder) if listdir is not None else []:
            if not name.endswith(".sii"):
                continue
            text = (read(f"{folder}/{name}") or b"").decode("utf-8", "replace")
            fits = _SUITABLE_RE.findall(text)
            m = _EXTERIOR_RE.search(text)
            if m and (not fits or cabin_id in fits) and m.group(1) not in defaults + others:
                others.append(m.group(1))
                kinds.setdefault(m.group(1), t)
    locs = pmg.locators(cab.model)

    def load_all(paths: list[str], paint_only: bool) -> list[CabinModel]:
        out = []
        for path in paths:
            try:
                acc = load_cabin(read, path)
            except ValueError as e:  # one broken accessory must not stop the body
                log.info("skipping accessory %s: %s", path, e)
                continue
            loc = locs.get(kinds.get(path, ""))
            if loc is not None:  # accessory models are drawn around their locator
                for p in acc.model.pieces:
                    p.positions = loc.apply(p.positions).astype("f4")
                    p.normals = loc.apply(p.normals, normals=True).astype("f4")
            if not paint_only or acc.pieces(is_paint):
                out.append(acc)
        return out

    body = Body(cab, [a for a in load_all(defaults, False) if _on_body(a, cab)], load_all(others, True))
    body.default_defs = default_defs
    return body


def _on_body(acc: CabinModel, cab: CabinModel, margin: float = 0.5) -> bool:
    """True if the accessory lies on the outside of the cabin (within ``margin`` metres of its box).

    Some default accessories (destination displays, interior parts) are placed by a locator
    elsewhere on the coach; drawn in cabin coordinates they would float off the body.
    """
    pos = [p.positions for p in acc.model.pieces if len(p.positions)]
    if not pos:
        return False
    a = np.concatenate(pos)
    c = np.concatenate([p.positions for p in cab.model.pieces if len(p.positions)])
    if _no_film(acc.pmd_path) or "mirror" in acc.pmd_path.replace("\\", "/").rsplit("/", 2)[-2]:
        # lamps, logos, badges and mirrors are placed by locators; drawn here they float in the views
        return False
    return bool(
        (a.min(axis=0) >= c.min(axis=0) - margin).all() and (a.max(axis=0) <= c.max(axis=0) + margin).all()
    )


def paint_region(cab: CabinModel | Body) -> Region | None:
    """Return the tile of the paint UV space the cabin's paint pieces use, or None.

    Paint pieces carry several UV sets (detail, normal map, paint job); the paint job set is the
    one that fits inside one tile.
    """
    model = cab.cab if isinstance(cab, Body) else cab
    for p in model.pieces(is_paint):
        for uv in p.uvs:
            for tile in TILES:
                if _inside(uv, tile):
                    return tile
    return None


# ---------------------------------------------------------------------------------- blueprint layout
# The five views of the blueprint. Each maps a face pointing along `normal` to the image,
# seen from outside: (normal axis, sign, u axis, u sign, v axis, v sign). v grows downwards.
_VIEWS = {
    "left": (0, -1, 2, +1, 1, -1),
    "right": (0, +1, 2, -1, 1, -1),
    "front": (2, -1, 0, -1, 1, -1),
    "rear": (2, +1, 0, +1, 1, -1),
    "top": (1, +1, 2, +1, 0, -1),  # seen from above, front on the left, right side up
}


@dataclass
class Layout:
    """Where each view of the coach sits in the blueprint (all in pixels)."""

    scale: float  # pixels per metre
    origin: dict[str, tuple[float, float]]  # view -> pixel of the view's top-left corner
    lo: np.ndarray  # bounding box of the body
    hi: np.ndarray

    @classmethod
    def for_body(cls, body: Body | CabinModel) -> Layout:
        """Lay out the five views for the body's size (cabin plus default accessories).

        Rows: left side + front, right side + rear, top. A 40 px gap separates the views.
        """
        models = [body.cab, *body.defaults] if isinstance(body, Body) else [body]
        allpos = np.concatenate([p.positions for m in models for p in m.model.pieces if len(p.positions)])
        lo, hi = allpos.min(axis=0), allpos.max(axis=0)
        size = hi - lo  # x width, y height, z length
        gap = 40
        w_m, h_m = size[2] + size[0], 2 * size[1] + size[0]
        scale = min((BLUEPRINT_SIZE[0] - 3 * gap) / w_m, (BLUEPRINT_SIZE[1] - 4 * gap) / h_m)
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
        """Return the blueprint pixel (u, v) of model-space points ``pos`` in ``view``."""
        _, _, ua, us, va, vs = _VIEWS[view]
        u0 = self.lo[ua] if us > 0 else self.hi[ua]  # the box edge at the view's top-left
        v0 = self.lo[va] if vs > 0 else self.hi[va]
        ox, oy = self.origin[view]
        return np.c_[(pos[:, ua] - u0) * us * self.scale + ox, (pos[:, va] - v0) * vs * self.scale + oy]

    def project_faces(self, views: list[str], pos: np.ndarray) -> np.ndarray:
        """Project points, each in the view of its face (``views`` has one entry per point)."""
        out = np.zeros((len(pos), 2))
        flat = np.asarray(views)
        for v in _VIEWS:
            sel = flat == v
            if sel.any():
                out[sel] = self.project(v, pos[sel])
        return out


def _face_views(p: pmg.Piece, centre: np.ndarray | None = None) -> list[str]:
    """The view each triangle of ``p`` belongs to, from its face normal (bottom faces go to top).

    With ``centre`` (the middle of the body's box), normals that point towards it are turned
    round first: some pieces store inward normals, which would put them in the opposite view.
    """
    a, b, c = (p.positions[p.triangles[:, k]] for k in range(3))
    n = np.cross(b - a, c - a)
    # the stored winding may be either way; trust the vertex normals' direction
    vn = p.normals[p.triangles].sum(axis=1)
    n *= np.sign((n * vn).sum(axis=1, keepdims=True) + 1e-12)
    if centre is not None:
        n *= np.where((n * ((a + b + c) / 3 - centre)).sum(axis=1, keepdims=True) < 0, -1.0, 1.0)
    axis = np.abs(n).argmax(axis=1)
    sign = np.sign(n[np.arange(len(n)), axis])
    by_axis = {(na, ns): v for v, (na, ns, *_) in _VIEWS.items()}
    return ["top" if ax == 1 else by_axis[(int(ax), int(sg) or 1)] for ax, sg in zip(axis, sign, strict=True)]


def film_piece(
    src: pmg.Piece, layout: Layout, material: int, sides: tuple[float, ...] = (1.0,)
) -> list[pmg.Piece]:
    """Make the film pieces over ``src``: its triangles, lifted off the surface, with blueprint UVs.

    Every triangle gets its own three vertices, so the UVs of neighbouring triangles seen in
    different views don't have to agree. ``sides`` says which copies to make: ``1.0`` lifted
    outwards and facing out, ``-1.0`` lifted inwards and facing in (seen from inside). Pieces
    are split to stay under 65535 vertices.
    """
    tri = src.triangles
    pos = src.positions[tri].reshape(-1, 3)
    nrm = src.normals[tri].reshape(-1, 3).astype(float)
    # Lift away from the body: some trim pieces store their normals pointing inwards, which put
    # their films under the trim. "Outwards" is away from the middle of the body's box.
    centre = (layout.lo + layout.hi) / 2
    inward = (nrm * (pos - centre)).sum(axis=1) < 0
    nrm[inward] *= -1
    # SCS vertex colours darken by baked shading (0x80 = unchanged); glass often carries black,
    # which would darken the artwork, so films use the colour of the body paint instead
    rgba = np.full(len(pos), FILM_RGBA, dtype=np.uint32)
    uv = layout.project_faces(list(np.repeat(_face_views(src, centre), 3)), pos) / np.array(
        BLUEPRINT_SIZE, float
    )

    def build(sign: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        t = np.arange(len(pos)).reshape(-1, 3)
        return pos + nrm * FILM_LIFT * sign, nrm * sign, t if sign > 0 else t[:, ::-1]

    out = []
    per = (0xFFFF // 3) * 3  # vertices per output piece, whole triangles only
    for p, n, t in [build(s) for s in sides]:
        for start in range(0, len(p), per):
            sl = slice(start, min(start + per, len(p)))
            out.append(
                pmg.Piece(
                    material,
                    p[sl].astype("f4"),
                    n[sl].astype("f4"),
                    [uv[sl].astype("f4")],
                    rgba[sl],
                    t[start // 3 : sl.stop // 3] - start,
                )
            )
    return out


# ---------------------------------------------------------------------------------- templates
def uv_template(body: Body | CabinModel, region: Region) -> Image.Image:
    """Draw the paint UV layout of the body (cabin and painted accessories) in its region.

    For people who prefer painting on the game's own layout; the blueprint is the easier way.
    """
    body = body if isinstance(body, Body) else Body(body, [], [])
    img = Image.new("RGBA", (region.w, region.h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for m in [body.cab, *body.defaults]:
        for p, px in paint_uvs(m, region):
            for t in p.triangles:
                d.polygon([tuple(px[k]) for k in t], fill=(128, 128, 128, 255), outline=(235, 235, 235, 255))
    return img


body_template = uv_template  # 0.12.0 name


def blueprint_template(body: Body | CabinModel) -> Image.Image:
    """Draw the blueprint: flat views of the coach from the left, right, front, rear and top.

    Body paint is grey, painted accessories lighter grey, windows blue and gloss-black parts
    yellow. Everything not drawn is transparent. One image painted on this template colours the
    paint job, the accessories, and (with the option) the windows and trim, all lined up.
    """
    body = body if isinstance(body, Body) else Body(body, [], [])
    layout = Layout.for_body(body)
    img = Image.new("RGBA", BLUEPRINT_SIZE, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    layers: list[tuple[list[pmg.Piece], tuple[int, ...], tuple[int, ...]]] = [
        (body.cab.pieces(is_paint), (85, 85, 85, 255), (150, 150, 150, 255)),
        ([p for m in body.defaults for p in m.pieces(is_paint)], (120, 120, 120, 255), (200, 200, 200, 255)),
        (body.pieces(is_gloss_black), (60, 50, 0, 255), (255, 200, 0, 255)),
        (body.pieces(is_window), (20, 50, 90, 255), (90, 170, 255, 255)),
    ]
    for pieces, fill, line in layers:
        for p in pieces:
            for piece in film_piece(p, layout, 0):
                px = piece.uvs[0] * BLUEPRINT_SIZE
                for t in piece.triangles:
                    d.polygon([tuple(px[k]) for k in t], fill=fill, outline=line)
    for view, (ox, oy) in layout.origin.items():
        d.text((ox, oy - 30), view.upper(), fill=(255, 255, 255, 255))
    return img


film_template = blueprint_template  # 0.12.0 name


# ---------------------------------------------------------------------------------- baking
def bake(body: Body, region: Region, blueprint: Image.Image, pad: int = 8) -> Image.Image:
    """Turn a blueprint image into the paint job texture of ``region``.

    Every texel of the paint UV layout is traced back to its point on the coach (through the
    triangle it lies in), and that point to the blueprint (through the view its face looks
    into). Texels outside every island stay transparent; the islands are then grown by ``pad``
    pixels so texture filtering doesn't pull transparent pixels into their edges.

    Accessories other than the defaults are painted first, so where their UVs share space with
    the defaults' or the cabin's, the cabin and defaults win.

    Args:
        body: The body (cabin and accessories).
        region: The cabin's tile of the paint UV space; the result has its size.
        blueprint: The user's image, drawn on :func:`blueprint_template` (any size; it is scaled
            to :data:`BLUEPRINT_SIZE`).
        pad: How many pixels the islands are grown by.
    """
    bp = np.asarray(blueprint.convert("RGBA").resize(BLUEPRINT_SIZE, Image.Resampling.LANCZOS))
    layout = Layout.for_body(body)
    centre = (layout.lo + layout.hi) / 2
    tris_uv, tris_pos, tris_nrm = [], [], []
    for m in [*body.others, *body.defaults, body.cab]:
        for p, px in paint_uvs(m, region):
            tris_uv.append(px[p.triangles])
            tris_pos.append(p.positions[p.triangles].astype(float))
            tris_nrm.append(p.normals[p.triangles].astype(float))
    w, h = region.w, region.h
    if not tris_uv:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    uv = np.concatenate(tris_uv)  # (n, 3, 2)
    pos = np.concatenate(tris_pos)  # (n, 3, 3)

    # 1. which triangle covers each texel (later triangles win, hence the order above)
    ids = Image.new("I", (w, h), 0)
    d = ImageDraw.Draw(ids)
    for i, t in enumerate(uv, start=1):
        d.polygon([tuple(c) for c in t], fill=i, outline=i)
    idmap = np.asarray(ids, dtype=np.int64)
    ys, xs = np.nonzero(idmap)
    t = idmap[ys, xs] - 1

    # 2. barycentric coordinates of each texel centre in its triangle -> point on the coach
    a, b, c = uv[t, 0], uv[t, 1], uv[t, 2]
    p = np.c_[xs + 0.5, ys + 0.5]
    v0, v1, v2 = b - a, c - a, p - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    den[np.abs(den) < 1e-12] = 1e-12  # degenerate (zero-area) triangles
    l1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
    l2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
    l0 = 1 - l1 - l2
    world = pos[t, 0] * l0[:, None] + pos[t, 1] * l1[:, None] + pos[t, 2] * l2[:, None]

    # 3. point on the coach -> blueprint pixels. Each texel is seen from the views its surface
    # faces, blended by how squarely it faces each one, so stripes fade across rounded corners
    # instead of breaking into chunks where the triangles change view.
    nrm = np.concatenate(tris_nrm)
    n = nrm[t, 0] * l0[:, None] + nrm[t, 1] * l1[:, None] + nrm[t, 2] * l2[:, None]
    n *= np.where((n * (world - centre)).sum(axis=1, keepdims=True) < 0, -1.0, 1.0)  # outwards
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    out = np.zeros((h, w, 4), np.uint8)
    out[ys, xs] = _triplanar(bp, layout, world, n)
    covered = np.zeros((h, w), bool)
    covered[ys, xs] = True
    return Image.fromarray(_pad(out, covered, pad), "RGBA")


#: How sharply the views are blended at corners (higher = narrower blend).
BLEND_POWER = 6


def _triplanar(bp: np.ndarray, layout: Layout, world: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Blueprint colour of each point, blended from the side, end and top views by its normal.

    Args:
        bp: The blueprint, (height, width, 4) uint8.
        layout: Where the views sit in it.
        world: (k, 3) points on the coach.
        n: (k, 3) outward unit normals.

    Returns:
        (k, 4) uint8 colours (straight alpha).
    """
    acc = np.zeros((len(world), 4))
    wsum = np.zeros(len(world))
    choices = (
        (0, np.where(n[:, 0] < 0, "left", "right")),
        (2, np.where(n[:, 2] < 0, "front", "rear")),
        (1, np.full(len(n), "top")),  # bottom faces use the top view too
    )
    for axis, views in choices:
        wgt = np.abs(n[:, axis]) ** BLEND_POWER
        sel = wgt > 1e-4
        if not sel.any():
            continue
        xy = layout.project_faces(list(views[sel]), world[sel])
        bx = np.clip(xy[:, 0].astype(int), 0, BLUEPRINT_SIZE[0] - 1)
        by = np.clip(xy[:, 1].astype(int), 0, BLUEPRINT_SIZE[1] - 1)
        c = bp[by, bx].astype(float)
        a = c[:, 3:4] / 255
        acc[sel, :3] += wgt[sel, None] * a * c[:, :3]  # premultiplied, so empty areas don't darken
        acc[sel, 3] += wgt[sel] * a[:, 0]
        wsum[sel] += wgt[sel]
    wsum[wsum == 0] = 1
    alpha = acc[:, 3] / wsum
    rgb = acc[:, :3] / np.maximum(acc[:, 3:4], 1e-6)
    return np.c_[np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)].round().astype(np.uint8)


def _pad(img: np.ndarray, covered: np.ndarray, steps: int) -> np.ndarray:
    """Grow the covered texels outwards by ``steps`` pixels, copying the nearest edge colour."""
    img, covered = img.copy(), covered.copy()
    for _ in range(steps):
        grown = covered.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            src = np.roll(covered, (dy, dx), axis=(0, 1))
            new = src & ~grown
            if new.any():
                img[new] = np.roll(img, (dy, dx), axis=(0, 1))[new]
                grown |= new
        covered = grown
    return img


# ---------------------------------------------------------------------------------- films
#: The accessory slot the films go into: the coach's window frame. Every coach has one fitted as
#: standard, and a paint job's ``overrides[]`` only replaces an accessory of a slot the coach
#: already has, so the films travel as a window frame (the standard frame's own model plus the
#: films) that the paint job puts on.
FILM_TYPE = "f_wnd_frame"

FILM_MAT = """effect : "eut2.dif.spec.weight.decal.over.rfx" {{
\tadditional_ambient : 0.000000
\tdiffuse : {{ 1.000000 , 1.000000 , 1.000000 }}
\treflection : 0.000000
\tshininess : {shininess:.6f}
\tspecular : {{ {spec:.6f} , {spec:.6f} , {spec:.6f} }}
\tqueue_bias : 1
\ttexture : "texture_base" {{
\t\tsource : "{tobj}"
\t\tsampler : default
\t}}
}}
"""


def film_material(tobj: str, glossy: bool) -> bytes:
    """The ``.mat`` text of a film: blended over what is behind it by the texture's alpha.

    Blending (not alpha testing) keeps the soft edges of the artwork, as on the painted body,
    and lets the inside window film let half the light through.
    """
    shininess, spec = (250.0, 1.0) if glossy else (95.0, 0.3)
    return FILM_MAT.format(shininess=shininess, spec=spec, tobj=tobj).encode()


def _no_film(pmd_path: str) -> bool:
    """True for accessories that never get films: lamps, logos and badges (by their folder)."""
    folder = pmd_path.replace("\\", "/").rsplit("/", 2)[-2]
    return any(k in folder for k in ("light", "logo", "badge"))


@dataclass
class Films:
    """The window frame with films of one body (see :data:`FILM_TYPE`).

    Attributes:
        exterior: ``(pmd, pmg)``: the standard window frame model with the outside films added:
            window films (opaque where painted) and trim films, facing out.
        interior: ``(pmd, pmg)`` of the inside window films (half the light through), facing in,
            or None when the body has no windows. The game draws it in the cab view.
        frame_def: Path of the standard frame's definition (``/def/...sii``); its icon and look
            are reused.
    """

    exterior: tuple[bytes, bytes]
    interior: tuple[bytes, bytes] | None
    frame_def: str


def films(body: Body, glass_out: str, trim: str, glass_in: str) -> Films | None:
    """Build the window and trim films of a body, as a window frame accessory.

    The films are copies of the glass and gloss-black pieces, lifted off the surface, with UVs
    on the blueprint layout of the whole body, so they line up with the paint job baked from the
    same image. Windows come from the cabin only (accessory "glass" is lamp lenses and mirror
    glass); trim comes from the cabin and its default accessories, except lamps, logos and badges.
    They are moved into the space of the cabin's window frame locator.

    Args:
        body: The body.
        glass_out: Mod path of the outside window film material (``/...mat``).
        trim: Mod path of the trim film material.
        glass_in: Mod path of the inside window film material.

    Returns:
        The films, or None when the body has no standard window frame or nothing to cover.
    """
    host_def = body.default_defs.get(FILM_TYPE)
    loc = pmg.locators(body.cab.model).get(FILM_TYPE)
    host = next((m for m in body.defaults if host_def and m.pmd_path == host_def[1]), None)
    if host_def is None or loc is None or host is None:
        return None
    layout = Layout.for_body(body)
    n = len(pmd.material_table(host.pmd_data)[0])
    outer: list[pmg.Piece] = []
    inner: list[pmg.Piece] = []
    for p in body.cab.pieces(is_window):
        outer += film_piece(p, layout, n, sides=(1.0,))
        inner += film_piece(p, layout, 0, sides=(-1.0,))
    for m in [body.cab, *body.defaults]:
        if m is body.cab or not _no_film(m.pmd_path):
            outer += [f for p in m.pieces(is_gloss_black) for f in film_piece(p, layout, n + 1, sides=(1.0,))]
    if not outer:
        return None
    for f in outer + inner:  # cabin space -> the frame's own space
        f.positions = loc.unapply(f.positions).astype("f4")
        f.normals = loc.unapply(f.normals, normals=True).astype("f4")
    return Films(
        (pmd.add_materials(host.pmd_data, [glass_out, trim]), pmg.add_pieces(host.model, outer)),
        (pmd.new_model([glass_in]), pmg.new_model(inner)) if inner else None,
        host_def[0],
    )


def write_templates(read: Reader, vehicles: Iterable[object], out_dir: Path) -> list[Path]:
    """Write the templates of every bus body into ``out_dir/<Brand Model>/``.

    Two PNGs per body: ``<body> blueprint.png`` (the one to paint) and ``<body> UV layout.png``
    (the game's own layout, for advanced use). Bodies without a model or region are skipped.

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
            body = load_body(read, v.id, x.id, x.model)  # type: ignore[attr-defined]
            folder.mkdir(parents=True, exist_ok=True)
            label = safe_name(x.name)
            for suffix, img in (
                ("blueprint", blueprint_template(body)),
                ("UV layout", uv_template(body, region)),
            ):
                path = folder / f"{label} {suffix}.png"
                img.save(path)
                written.append(path)
    return written
