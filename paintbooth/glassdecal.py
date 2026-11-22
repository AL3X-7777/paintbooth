"""Windscreen decals and door window art for any truck, built from the game's own models.

Mods Studio 2's "Truck windscreen decal" and "Truck door window" templates ship ready-made models
for a fixed list of trucks. Paintbooth adds every other truck of the game by building the
same things from the truck's own files at export time (the game folder from Settings):

* **Windscreen decal**: the front-facing triangles of the cabin's outside windscreen pane,
  copied and lifted 0.5 mm off the glass, with UVs from a straight front view. It is an
  accessory in the ``set_glass`` slot (the windscreen toys slot, as Mods Studio 2 uses), so it is
  bought in the truck shop. The outside copy has an alpha-tested core and a blended layer (as the
  coach films, see :mod:`paintbooth.coach`); the inside copy is seen from the cab.
* **Door window**: the truck's opening side windows (``windows_model``) get the artwork as their
  glass texture. The window model is written back with new UVs from a side view (so the image
  is not tiled across the glass as the glass texture is) and new materials; its skinning stays,
  so the art moves with the window. This changes the windows of every truck that shares that
  model, as Mods Studio 2's door windows do.

Each truck's template (the outline to paint on) is written by :func:`write_templates`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import pmd, pmg
from .coach import FILM_LIFT, FILM_RGBA, CabinModel, load_cabin

Reader = Callable[[str], bytes | None]

#: Size of the templates and of the textures made from them.
TEMPLATE_SIZE = (2048, 1024)
#: Accessory slots a windscreen decal can hang on, most wanted first (cabin locators).
HOOKUPS = ("set_glass", "frntglss_mid")
#: Prefix of the truck choices Paintbooth adds to the two Mods Studio 2 templates.
NATIVE_PREFIX = "pb_"
#: Raise when the templates change, so start-up writes them again.
TEMPLATE_VERSION = 1

_UNIT_RE = re.compile(r"accessory_cabin_data\s*:\s*([\w.]+)")
_MODEL_RE = re.compile(r'(?m)^\s*model\s*:\s*"([^"]+)"')
_DETAIL_RE = re.compile(r'(?m)^\s*detail_model\s*:\s*"([^"]+)"')
_WINDOWS_RE = re.compile(r'(?m)^\s*windows_model\s*:\s*"([^"]+)"')
_TEX_RE = re.compile(r'(texture\s*:\s*"texture_base"\s*\{[^}]*?source\s*:\s*")([^"]*)(")', re.S)


@dataclass
class Cabin:
    """One cabin of a truck: its unit name and the models the decals are made from."""

    id: str  # e.g. "l2h1.volvo.fh_2024.cabin"
    model: str  # the cabin model (.pmd)
    windows: str | None  # the opening windows model (.pmd), if any


def native_value(vehicle_id: str) -> str:
    """The option value of a truck Paintbooth builds itself (``pb_volvo_fh_2024``)."""
    return NATIVE_PREFIX + vehicle_id.replace(".", "_")


def is_native(value: str) -> bool:
    """True for a truck choice that Paintbooth builds itself (see :func:`native_value`)."""
    return value.startswith(NATIVE_PREFIX)


def truck_cabins(read: Reader, vehicle_id: str) -> list[Cabin]:
    """The cabins of ``vehicle_id`` with their models, from its ``def/.../cabin/*.sii`` files.

    ``read`` must be able to list folders (``read.listdir``), as :func:`scanner.game_reader` can.
    """
    listdir: Callable[[str], list[str]] | None = getattr(read, "listdir", None)
    folder = f"def/vehicle/truck/{vehicle_id}/cabin"
    out = []
    for name in sorted(listdir(folder) or []) if listdir else []:
        if not name.endswith(".sii"):
            continue
        text = (read(f"{folder}/{name}") or b"").decode("utf-8", "replace")
        unit = _UNIT_RE.search(text)
        model = _MODEL_RE.search(text) or _DETAIL_RE.search(text)
        if unit and model:
            win = _WINDOWS_RE.search(text)
            out.append(Cabin(unit.group(1), model.group(1), win.group(1) if win else None))
    return out


# ---------------------------------------------------------------------------------- geometry
def _oriented_normals(p: pmg.Piece) -> np.ndarray:
    """Unit face normals of ``p``'s triangles, turned to agree with its vertex normals."""
    a, b, c = (p.positions[p.triangles[:, k]].astype(float) for k in range(3))
    n = np.cross(b - a, c - a)
    vn = p.normals[p.triangles].sum(axis=1)
    n *= np.sign((n * vn).sum(axis=1, keepdims=True) + 1e-12)
    return n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)


def _area(p: pmg.Piece, tris: np.ndarray) -> float:
    a, b, c = (p.positions[tris[:, k]].astype(float) for k in range(3))
    return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())


def _glass(cab: CabinModel, outside: bool = True) -> list[pmg.Piece]:
    """The glass pieces of a model; with ``outside``, not the inner panes (``*_int`` textures)."""
    out, seen = [], set()
    for p in cab.model.pieces:
        m = cab.mats[p.material]
        if "glass" not in m.effect or (outside and "int" in m.texture.rsplit("/", 1)[-1]):
            continue
        key = (len(p.positions), round(float(p.positions.sum()), 3))  # LODs and looks repeat pieces
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _largest_part(p: pmg.Piece, tris: np.ndarray) -> np.ndarray:
    """The triangles of the largest connected surface among ``tris`` (by area).

    Windscreen panes often share their piece with small bits of glass (mirror cameras, a roof
    window strip); only the windscreen itself is wanted.
    """
    if not len(tris):
        return tris
    # vertices at the same place count as one (seams split them)
    keys = np.unique(np.round(p.positions * 1000).astype(np.int64), axis=0, return_inverse=True)[1].ravel()
    parent = list(range(int(keys.max()) + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, c in keys[tris]:
        ra, rb, rc = find(int(a)), find(int(b)), find(int(c))
        parent[rb] = ra
        parent[find(rc)] = ra
    roots = np.array([find(int(keys[t[0]])) for t in tris])
    a, b, c = (p.positions[tris[:, k]].astype(float) for k in range(3))
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    best = max(set(roots.tolist()), key=lambda r: area[roots == r].sum())
    return tris[roots == best]


def windscreen(cab: CabinModel) -> pmg.Piece | None:
    """The windscreen: the largest front-facing surface of the outside glass, or None."""
    best: tuple[float, pmg.Piece] | None = None
    for p in _glass(cab):
        front = _largest_part(p, p.triangles[_oriented_normals(p)[:, 2] < -0.5])  # -z is forward
        if len(front) and (best is None or _area(p, front) > best[0]):
            best = (_area(p, front), pmg.Piece(p.material, p.positions, p.normals, p.uvs, p.rgba, front))
    return best[1] if best else None


@dataclass
class View:
    """A flat view fitted into the template: which axes, which way, and the fit."""

    u_axis: int
    u_sign: float
    v_axis: int
    lo: np.ndarray
    hi: np.ndarray
    scale: float
    offset: np.ndarray

    @classmethod
    def fit(cls, pos: np.ndarray, u_axis: int, u_sign: float, v_axis: int = 1, margin: float = 0.04) -> View:
        """Fit the points' box, seen along the remaining axis, into :data:`TEMPLATE_SIZE`."""
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        w, h = TEMPLATE_SIZE
        span = np.array([hi[u_axis] - lo[u_axis], hi[v_axis] - lo[v_axis]]) + 1e-9
        scale = float(min(w * (1 - 2 * margin) / span[0], h * (1 - 2 * margin) / span[1]))
        offset = (np.array([w, h]) - span * scale) / 2
        return cls(u_axis, u_sign, v_axis, lo, hi, scale, offset)

    def pixels(self, pos: np.ndarray) -> np.ndarray:
        """Template pixel of each point (``u_sign`` -1 mirrors the view left to right)."""
        a = self.u_axis
        u = (pos[:, a] - self.lo[a]) if self.u_sign > 0 else (self.hi[a] - pos[:, a])
        v = self.hi[self.v_axis] - pos[:, self.v_axis]  # up is up
        return np.c_[u, v] * self.scale + self.offset

    def uvs(self, pos: np.ndarray) -> np.ndarray:
        """Texture coordinates (0-1) of each point."""
        return self.pixels(pos) / np.array(TEMPLATE_SIZE, float)


def _front_view(piece: pmg.Piece) -> View:
    """Front view of the windscreen: the truck's right side on the image's left."""
    return View.fit(piece.positions[np.unique(piece.triangles)].astype(float), 0, -1.0)


def _films(piece: pmg.Piece, view: View, material: int, sign: float, lift: float) -> pmg.Piece:
    """One lifted copy of ``piece``'s triangles with UVs from ``view`` (``sign`` -1 = inside, facing in)."""
    tri = piece.triangles
    pos = piece.positions[tri].reshape(-1, 3).astype(float)
    nrm = np.repeat(_oriented_normals(piece), 3, axis=0)
    t = np.arange(len(pos)).reshape(-1, 3)
    return pmg.Piece(
        material,
        (pos + nrm * lift * sign).astype("f4"),
        (nrm * sign).astype("f4"),
        [view.uvs(pos).astype("f4")],
        np.full(len(pos), FILM_RGBA, dtype=np.uint32),
        t if sign > 0 else t[:, ::-1],
    )


def windscreen_models(
    cab: CabinModel, core: str, over: str, inside: str
) -> tuple[str, tuple[bytes, bytes], tuple[bytes, bytes]] | None:
    """Build a windscreen decal's models for one cabin model.

    Args:
        cab: The cabin model.
        core: Mod path of the outside alpha-tested material (``.mat``).
        over: Mod path of the outside blended material.
        inside: Mod path of the inside material.

    Returns:
        ``(hookup, (ext pmd, ext pmg), (int pmd, int pmg))`` in the hookup locator's space, or
        None when the cabin has no windscreen or no decal slot.
    """
    glass = windscreen(cab)
    locs = pmg.locators(cab.model)
    hookup = next((h for h in HOOKUPS if h in locs), None)
    if glass is None or hookup is None:
        return None
    view = _front_view(glass)
    outside = [_films(glass, view, 0, 1.0, FILM_LIFT), _films(glass, view, 1, 1.0, FILM_LIFT * 1.3)]
    inner = [_films(glass, view, 0, -1.0, FILM_LIFT)]
    loc = locs[hookup]
    for f in outside + inner:
        f.positions = loc.unapply(f.positions).astype("f4")
        f.normals = loc.unapply(f.normals, normals=True).astype("f4")
    return (
        hookup,
        (pmd.new_model([core, over]), pmg.new_model(outside)),
        (pmd.new_model([inside]), pmg.new_model(inner)),
    )


# ---------------------------------------------------------------------------------- door windows
def _side_views(pieces: list[pmg.Piece]) -> tuple[View, View] | None:
    """Side views of the left and right windows, laid out so artwork reads right on both sides.

    Seen from outside, the left window has the front on the image's left and the right window
    on the image's right, so one image reads the right way round on both doors.
    """
    pts = [p.positions[np.unique(p.triangles)].astype(float) for p in pieces if len(p.triangles)]
    if not pts:
        return None
    allp = np.concatenate(pts)
    left = allp[allp[:, 0] < 0]
    right = allp[allp[:, 0] >= 0]
    ref = left if len(left) else right
    lv = View.fit(ref, 2, +1.0)  # left side: -z (front) on the left
    rv = View.fit(right if len(right) else ref, 2, -1.0)  # right side: front on the right
    return lv, rv


def door_window_model(win: CabinModel, mats: dict[str, str]) -> tuple[bytes, bytes] | None:
    """The opening windows model with the artwork as its glass, or None when it has no glass.

    Args:
        win: The windows model, loaded from the game.
        mats: Original glass material path -> the replacement material path.

    Returns:
        ``(pmd, pmg)``: the material table renamed, the glass pieces' first UV set replaced by
        side-view UVs (left and right windows each fitted to the template).
    """
    glass = [p for p in win.model.pieces if "glass" in win.mats[p.material].effect]
    views = _side_views(glass)
    if views is None:
        return None
    lv, rv = views
    uvs = {}
    for i, p in enumerate(win.model.pieces):
        if any(p is g for g in glass) and p.uvs:
            side = p.positions[:, 0] >= 0
            uv = lv.uvs(p.positions.astype(float))
            uv[side] = rv.uvs(p.positions[side].astype(float))
            uvs[i] = uv
    return pmd.rewrite(win.pmd_data, mats), pmg.with_uvs(win.model, uvs)


_DIFFUSE_RE = re.compile(r"diffuse\s*:\s*\{[^}]*\}")


def glass_material(original: str, tobj: str) -> str:
    """A glass material's text with its base texture replaced by ``tobj``.

    The diffuse colour is set to white: SCS glass is often nearly black there, which would turn
    the artwork black. Everything else (tint, reflection, fresnel) is kept.
    """
    original = _DIFFUSE_RE.sub("diffuse : { 1.000000 , 1.000000 , 1.000000 }", original, count=1)
    if _TEX_RE.search(original):
        return _TEX_RE.sub(lambda m: m.group(1) + tobj + m.group(3), original, count=1)
    return original.rstrip().rstrip("}") + f'\ttexture : "texture_base" {{\n\t\tsource : "{tobj}"\n\t}}\n}}\n'


# ---------------------------------------------------------------------------------- templates
def _draw(
    img: Image.Image, triangles: Iterable[np.ndarray], fill: tuple[int, ...], line: tuple[int, ...]
) -> None:
    d = ImageDraw.Draw(img)
    for t in triangles:
        d.polygon([tuple(c) for c in t], fill=fill, outline=line)


def windscreen_template(cab: CabinModel) -> Image.Image | None:
    """The windscreen seen from the front (as the decal is seen from outside), or None."""
    glass = windscreen(cab)
    if glass is None:
        return None
    view = _front_view(glass)
    img = Image.new("RGBA", TEMPLATE_SIZE, (0, 0, 0, 0))
    px = view.pixels(glass.positions.astype(float))
    _draw(img, (px[t] for t in glass.triangles), (90, 170, 255, 110), (90, 170, 255, 255))
    ImageDraw.Draw(img).text((12, 8), "WINDSCREEN, SEEN FROM THE FRONT", fill=(255, 255, 255, 255))
    return img


def door_window_template(win: CabinModel) -> Image.Image | None:
    """The left door window seen from outside (the right one is its mirror image), or None."""
    glass = [p for p in win.model.pieces if "glass" in win.mats[p.material].effect]
    views = _side_views(glass)
    if views is None:
        return None
    lv = views[0]
    img = Image.new("RGBA", TEMPLATE_SIZE, (0, 0, 0, 0))
    for p in glass:
        keep = (
            p.triangles[(p.positions[p.triangles][:, :, 0] < 0).all(axis=1)]
            if (p.positions[:, 0] < 0).any()
            else p.triangles
        )
        px = lv.pixels(p.positions.astype(float))
        _draw(img, (px[t] for t in keep), (90, 170, 255, 90), (90, 170, 255, 255))
    ImageDraw.Draw(img).text(
        (12, 8), "LEFT DOOR WINDOW, FRONT ON THE LEFT (MIRRORED ON THE RIGHT DOOR)", fill=(255, 255, 255, 255)
    )
    return img


def write_templates(read: Reader, trucks: Iterable[tuple[str, str]], out_dir: Path) -> list[Path]:
    """Write the windscreen and door window templates of each truck.

    Args:
        read: Reads (and lists) the game's files.
        trucks: ``(vehicle id, shown name)`` pairs.
        out_dir: The game's template folder; files go to ``Windscreen decals/<name>.png`` and
            ``Door windows/<name>.png``.

    Returns:
        The files written. Trucks whose models can't be read are skipped.
    """
    from .templates import safe_name

    written = []
    for vid, name in trucks:
        cabins = truck_cabins(read, vid)
        done: set[str] = set()
        for kind, path_of, make in (
            ("Windscreen decals", lambda c: c.model, windscreen_template),
            ("Door windows", lambda c: c.windows, door_window_template),
        ):
            models = []
            for c in cabins:
                p = path_of(c)
                if p and p not in done and p not in models:
                    models.append(p)
            unique: dict[bytes, tuple[str, Image.Image]] = {}  # cabins with the same glass: one template
            for path in models:
                try:
                    img = make(load_cabin(read, path))
                except ValueError:
                    continue
                if img is not None:
                    unique.setdefault(img.tobytes(), (path, img))
            for path, img in unique.values():
                stem = safe_name(name) + (f" ({Path(path).stem})" if len(unique) > 1 else "")
                f = out_dir / kind / f"{stem}.png"
                f.parent.mkdir(parents=True, exist_ok=True)
                img.save(f)
                written.append(f)
            done.update(models)
    return written


# ---------------------------------------------------------------------------------- the two items
#: The Mods Studio 2 template items Paintbooth adds trucks to: kind -> (truck variable, image variable).
ITEMS = {"TruckWindscreenDecal": ("Truck", "SkinImage"), "TruckDoorWindow": ("Truck", "WindowsSkin")}
#: Group shown for the added trucks in the truck list.
NATIVE_GROUP = "Paintbooth (from your game)"


def add_native_trucks(item_el: object, trucks: Iterable[tuple[str, str]]) -> int:
    """Add the game's trucks to a windscreen decal or door window item's truck list.

    Trucks the list already has (by vehicle id, in its ``InternalNames``) are left out. The new
    options' value is :func:`native_value`; their only child variable is ``InternalNames``.

    Args:
        item_el: The item's XML element (:class:`paintbooth.xmldom.Elem`).
        trucks: ``(vehicle id, shown name)`` pairs.

    Returns:
        How many options were added (0 when the list is complete or the item has none).
    """
    from .xmldom import E, Elem

    var = next(
        (v for v in getattr(item_el, "find")("Variables").children if v.get_text("Name") == "Truck"),  # noqa: B009
        None,
    )
    pre = var.find("PredefinedValues") if var is not None else None
    if pre is None:
        return 0
    have = {o.get_text("Value") for o in pre.children}
    for o in pre.children:
        for v in o.find("ChildVariables").children if o.find("ChildVariables") is not None else []:
            if v.get_text("Name") in ("InternalNames", "InternalName"):
                val = v.find("Value")
                have |= (
                    {s.text or "" for s in val.children}
                    if val is not None and val.children
                    else {v.get_text("Value")}
                )
    added = 0
    for vid, name in trucks:
        value = native_value(vid)
        if value in have or vid in have:
            continue
        opt = Elem(
            "anyType",
            None,
            {"xsi:type": "ListItemVariable"},
            [
                E(
                    "ChildVariables",
                    E(
                        "Variable",
                        E("ChildVariables"),
                        E("IsEnabled", text="true"),
                        E("Name", text="InternalNames"),
                        E("Value", E("string", text=vid)),
                        type="StringArrayVariable",
                    ),
                ),
                E("Group", text=NATIVE_GROUP),
                E("IsEnabled", text="true"),
                E("Text", text=name),
                E("Value", text=value),
                E("PredefinedValuesSelected", text="false"),
            ],
        )
        pre.children.append(opt)
        added += 1
    return added


def native_vehicle(item_el: object, value: str) -> str | None:
    """The vehicle id of the chosen native truck option ``value``, or None if it is not one."""
    if not is_native(value):
        return None
    var = next(
        (v for v in getattr(item_el, "find")("Variables").children if v.get_text("Name") == "Truck"),  # noqa: B009
        None,
    )
    pre = var.find("PredefinedValues") if var is not None else None
    for o in pre.children if pre is not None else []:
        if o.get_text("Value") == value:
            cv = o.find("ChildVariables")
            for v in cv.children if cv is not None else []:
                val = v.find("Value")
                if v.get_text("Name") == "InternalNames" and val is not None and val.children:
                    return val.children[0].text
    return value[len(NATIVE_PREFIX) :].replace("_", ".", 1)


def plan_windscreen(
    read: Reader, vid: str, n: str, name: str, price: str, unlock: str, image: Path, icon: Path | None
) -> list[object]:
    """The files of a windscreen decal ``n`` for truck ``vid`` (see the module docstring).

    Raises:
        ValueError: The truck has no cabin with a windscreen and a decal slot in the game files.
    """
    from .builder import OutFile, TextureJob
    from .coach import film_material
    from .formats import sii_file, sii_str, tobj_bytes, ui_mat

    root = f"vehicle/truck/upgrade/f_decals/pb/{n}"
    out: list[object] = [
        OutFile(f"{root}/decal.dds", job=TextureJob(image, "film", TEMPLATE_SIZE, "DXT5", None, 1.0)),
        OutFile(f"{root}/decal.tobj", tobj_bytes(f"/{root}/decal.dds", profile="skin")),
        OutFile(f"{root}/core.mat", film_material(f"/{root}/decal.tobj", False, core=True)),
        OutFile(f"{root}/over.mat", film_material(f"/{root}/decal.tobj", False)),
    ]
    if icon is not None:
        ui = f"material/ui/accessory/f_decals/pb/{n}"
        out += [
            OutFile(f"{ui}.dds", job=TextureJob(icon, "icon", (256, 64), "BGRA_SRGB")),
            OutFile(f"{ui}.tobj", tobj_bytes(f"/{ui}.dds", profile="ui")),
            OutFile(f"{ui}.mat", ui_mat(f"{n}.tobj").encode()),
        ]
    groups: dict[str, list[str]] = {}
    for c in truck_cabins(read, vid):
        groups.setdefault(c.model, []).append(c.id)
    units = []
    for k, (model, cabins) in enumerate(groups.items()):
        try:
            made = windscreen_models(
                load_cabin(read, model), f"/{root}/core.mat", f"/{root}/over.mat", f"/{root}/over.mat"
            )
        except ValueError:
            continue
        if made is None:
            continue
        hookup, ext, inner = made
        out += [
            OutFile(f"{root}/ext{k}.pmd", ext[0]),
            OutFile(f"{root}/ext{k}.pmg", ext[1]),
            OutFile(f"{root}/int{k}.pmd", inner[0]),
            OutFile(f"{root}/int{k}.pmg", inner[1]),
        ]
        unit = n if len(groups) == 1 else f"{n[:11]}{k}" if k < 10 else f"{n[:10]}{k}"
        attrs = [
            ("name", sii_str(name)),
            ("price", str(int(float(price or 0)))),
            ("unlock", str(int(float(unlock or 0)))),
            *([("icon", sii_str(f"f_decals/pb/{n}"))] if icon is not None else []),
            ("part_type", "aftermarket"),
            ("exterior_model", sii_str(f"/{root}/ext{k}.pmd")),
            ("interior_model", sii_str(f"/{root}/int{k}.pmd")),
            *[("suitable_for[]", sii_str(c)) for c in cabins],
        ]
        out.append(
            OutFile(
                f"def/vehicle/truck/{vid}/accessory/{hookup}/{unit}.sii",
                sii_file([("accessory_addon_data", f"{unit}.{vid}.{hookup}", attrs)]).encode(),
            )
        )
        units.append(unit)
    if not units:
        raise ValueError(f"no cabin of {vid} has a windscreen and a decal slot in the game files")
    return out


def plan_door_window(read: Reader, vid: str, image: Path) -> list[object]:
    """The files that put ``image`` on the door windows of truck ``vid``.

    Raises:
        ValueError: The truck has no opening windows model in the game files.
    """
    from .builder import OutFile, TextureJob
    from .formats import tobj_bytes

    out: list[object] = []
    windows = sorted({c.windows for c in truck_cabins(read, vid) if c.windows})
    for path in windows:
        try:
            win = load_cabin(read, path)
        except ValueError:
            continue
        folder = path.lstrip("/").rsplit("/", 1)[0]
        stem = Path(path).stem
        tex = f"{folder}/pb_{stem}_art"
        rename: dict[str, str] = {}
        for k, mat_path in enumerate(pmd.material_table(win.pmd_data)[0]):
            text = (read(mat_path.lstrip("/")) or b"").decode("utf-8", "replace")
            if mat_path in rename or "glass" not in text.split("{", 1)[0]:
                continue
            new = f"/{folder}/pb_{stem}_glass{k}.mat"
            rename[mat_path] = new
            out.append(OutFile(new.lstrip("/"), glass_material(text, f"/{tex}.tobj").encode()))
        made = door_window_model(win, rename)
        if made is None or not rename:
            continue
        rel = path.lstrip("/")
        out += [
            OutFile(rel, made[0]),
            OutFile(rel[: -len(".pmd")] + ".pmg", made[1]),
            OutFile(f"{tex}.dds", job=TextureJob(image, "film", TEMPLATE_SIZE, "DXT5", None, 1.0)),
            OutFile(f"{tex}.tobj", tobj_bytes(f"/{tex}.dds", profile="skin")),
        ]
    if not out:
        raise ValueError(f"{vid} has no opening door windows in the game files")
    return out
