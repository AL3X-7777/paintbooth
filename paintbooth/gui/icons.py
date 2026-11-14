"""Ribbon, toolbar and status icons, drawn in code with Pillow (no bundled artwork).

Each icon is drawn on a 32x32 design grid, 4x supersampled, then shrunk with Lanczos, so it stays
crisp at any size the UI scale asks for. :class:`Icons` caches the resulting ``PhotoImage`` objects
(Tk shows nothing if the Python object is garbage-collected), and :func:`shared` gives the one
cache the widgets use.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PIL import Image, ImageDraw, ImageTk

from .theme import SCALE

S = 4  # supersampling factor

RGBA = tuple[int, int, int, int]

BLUE: RGBA = (66, 141, 230, 255)
GREEN: RGBA = (78, 186, 112, 255)
RED: RGBA = (232, 92, 84, 255)
YELLOW: RGBA = (240, 190, 60, 255)
ORANGE: RGBA = (236, 140, 60, 255)
PURPLE: RGBA = (156, 110, 226, 255)
GREY: RGBA = (150, 156, 166, 255)
WHITE: RGBA = (240, 242, 246, 255)
LIGHT_GREY: RGBA = (200, 204, 212, 255)  # outline arrows and section triangles
PALE_BLUE: RGBA = (200, 225, 245, 255)  # page colour of the preview / model icons


def _canvas(size: int) -> Image.Image:
    """A transparent supersampled canvas for an icon of ``size`` pixels."""
    return Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))


def _done(img: Image.Image, size: int) -> Image.Image:
    """Shrink the supersampled drawing to its final size."""
    return img.resize((size, size), Image.Resampling.LANCZOS)


class _Pen:
    """Drawing context for one icon: the Pillow ``ImageDraw`` plus the grid-unit scale.

    Coordinates are given on the 32x32 design grid; :meth:`p` converts them to canvas pixels.
    """

    def __init__(self, d: ImageDraw.ImageDraw, u: float, name: str) -> None:
        self.d, self.u, self.name = d, u, name

    def p(self, *xy: float) -> list[float]:
        """Scale grid coordinates to canvas pixels."""
        return [v * self.u for v in xy]

    def w(self, grid: float) -> int:
        """A line width given in grid units, as whole canvas pixels."""
        return int(grid * self.u)

    def doc(self, x0: float, y0: float, x1: float, y1: float, fill: RGBA, fold: bool = True) -> None:
        """A page with a folded top-right corner (grid coordinates)."""
        x0, y0, x1, y1 = self.p(x0, y0, x1, y1)
        f = (x1 - x0) * 0.3
        self.d.polygon([(x0, y0), (x1 - f, y0), (x1, y0 + f), (x1, y1), (x0, y1)], fill=fill)
        if fold:
            self.d.polygon([(x1 - f, y0), (x1 - f, y0 + f), (x1, y0 + f)], fill=(255, 255, 255, 110))


# ----------------------------------------------------------------------------- project and items


def _new(k: _Pen) -> None:
    k.doc(7, 3, 25, 29, WHITE)
    k.d.ellipse(k.p(16, 16, 30, 30), fill=GREEN)
    k.d.rectangle(k.p(22, 19, 24, 27), fill=WHITE)
    k.d.rectangle(k.p(19, 22, 27, 24), fill=WHITE)


def _open(k: _Pen) -> None:
    k.d.rectangle(k.p(3, 8, 14, 12), fill=YELLOW)
    k.d.rectangle(k.p(3, 11, 29, 27), fill=(214, 160, 40, 255))
    k.d.polygon(k.p(6, 15, 31, 15, 27, 27, 3, 27), fill=YELLOW)


def _save(k: _Pen) -> None:
    """Floppy disk; ``saveas`` adds a pencil."""
    k.d.rounded_rectangle(k.p(4, 4, 28, 28), radius=3 * k.u, fill=BLUE)
    k.d.rectangle(k.p(9, 4, 23, 12), fill=WHITE)
    k.d.rectangle(k.p(9, 17, 23, 28), fill=(30, 60, 110, 255))
    if k.name == "saveas":
        k.d.polygon(k.p(18, 30, 30, 18, 32, 20, 20, 32), fill=YELLOW)


def _add(k: _Pen) -> None:
    k.d.rounded_rectangle(k.p(3, 5, 23, 27), radius=2 * k.u, fill=PURPLE)
    k.d.rectangle(k.p(7, 10, 19, 12), fill=WHITE)
    k.d.rectangle(k.p(7, 15, 16, 17), fill=WHITE)
    k.d.ellipse(k.p(17, 15, 31, 29), fill=GREEN)
    k.d.rectangle(k.p(23, 18, 25, 26), fill=WHITE)
    k.d.rectangle(k.p(20, 21, 28, 23), fill=WHITE)


def _duplicate(k: _Pen) -> None:
    k.d.rounded_rectangle(k.p(4, 4, 20, 22), radius=2 * k.u, fill=GREY)
    k.d.rounded_rectangle(k.p(12, 10, 28, 28), radius=2 * k.u, fill=BLUE)


def _delete(k: _Pen) -> None:
    k.d.rectangle(k.p(8, 9, 24, 29), fill=RED)
    k.d.rectangle(k.p(5, 6, 27, 9), fill=RED)
    k.d.rectangle(k.p(13, 3, 19, 6), fill=RED)
    for x in (12, 16, 20):
        k.d.rectangle(k.p(x - 0.8, 12, x + 0.8, 26), fill=(255, 255, 255, 170))


def _export(k: _Pen) -> None:
    k.d.rounded_rectangle(k.p(3, 9, 23, 29), radius=2 * k.u, fill=ORANGE)
    k.d.text((6 * k.u, 13 * k.u), "SCS", fill=WHITE)
    k.d.polygon(k.p(18, 3, 30, 3, 30, 15, 26, 11, 20, 17, 16, 13, 22, 7), fill=GREEN)


def _folder(k: _Pen) -> None:
    k.d.rectangle(k.p(3, 8, 14, 12), fill=YELLOW)
    k.d.rectangle(k.p(3, 11, 29, 27), fill=YELLOW)
    k.d.ellipse(k.p(17, 15, 29, 27), outline=WHITE, width=k.w(2))


# ----------------------------------------------------------------------------- tools


def _color(k: _Pen) -> None:
    for i, c in enumerate([RED, YELLOW, GREEN, BLUE]):
        k.d.pieslice(k.p(3, 3, 29, 29), start=i * 90, end=i * 90 + 90, fill=c)
    k.d.ellipse(k.p(11, 11, 21, 21), fill=WHITE)


def _extract(k: _Pen) -> None:
    k.d.rectangle(k.p(4, 12, 28, 28), fill=(214, 160, 40, 255))
    k.d.polygon(k.p(4, 12, 9, 6, 23, 6, 28, 12), fill=YELLOW)
    k.d.polygon(k.p(16, 1, 22, 7, 18, 7, 18, 12, 14, 12, 14, 7, 10, 7), fill=BLUE)


def _tobj(k: _Pen) -> None:
    k.doc(5, 2, 25, 30, WHITE)
    for y in (10, 15, 20):
        k.d.rectangle(k.p(9, y, 21, y + 1.5), fill=BLUE)
    k.d.polygon(k.p(17, 31, 29, 19, 31, 21, 19, 33), fill=BLUE)


def _vehicles(k: _Pen) -> None:
    k.d.rounded_rectangle(k.p(2, 10, 20, 24), radius=2 * k.u, fill=PURPLE)
    k.d.rounded_rectangle(k.p(19, 13, 30, 24), radius=2 * k.u, fill=(120, 80, 200, 255))
    k.d.rectangle(k.p(22, 15, 28, 19), fill=(200, 220, 255, 255))
    for x in (7, 24):
        k.d.ellipse(k.p(x - 3.5, 21, x + 3.5, 28), fill=(40, 40, 46, 255))


def _cargo(k: _Pen) -> None:
    k.d.rectangle(k.p(3, 9, 29, 24), fill=YELLOW)
    for x in range(6, 28, 5):
        k.d.rectangle(k.p(x, 9, x + 1.5, 24), fill=ORANGE)
    k.d.rectangle(k.p(3, 24, 29, 27), fill=GREY)


def _preview(k: _Pen) -> None:
    k.doc(5, 2, 25, 30, PALE_BLUE)
    k.d.ellipse(k.p(12, 13, 26, 27), outline=BLUE, width=k.w(2.5))
    k.d.line(k.p(24, 25, 30, 31), fill=BLUE, width=k.w(3))


def _uv(k: _Pen) -> None:
    k.d.ellipse(k.p(3, 3, 29, 29), outline=GREEN, width=k.w(2.5))
    k.d.line(k.p(3, 16, 29, 16), fill=GREEN, width=k.w(2))
    k.d.line(k.p(16, 3, 16, 29), fill=GREEN, width=k.w(2))
    k.d.arc(k.p(8, 3, 24, 29), 0, 360, fill=GREEN, width=k.w(2))


def _model(k: _Pen) -> None:
    k.doc(5, 2, 25, 30, PALE_BLUE)
    k.d.polygon(k.p(15, 10, 22, 14, 15, 18, 8, 14), fill=BLUE)
    k.d.polygon(k.p(8, 14, 15, 18, 15, 26, 8, 22), fill=(40, 100, 190, 255))
    k.d.polygon(k.p(22, 14, 15, 18, 15, 26, 22, 22), fill=(90, 160, 240, 255))


def _gear(k: _Pen) -> None:
    c = (190, 196, 206, 255)
    for i in range(8):
        a = i * math.pi / 4
        x, y = 16 + 11 * math.cos(a), 16 + 11 * math.sin(a)
        k.d.ellipse(k.p(x - 3.5, y - 3.5, x + 3.5, y + 3.5), fill=c)
    k.d.ellipse(k.p(6, 6, 26, 26), fill=c)
    k.d.ellipse(k.p(12, 12, 20, 20), fill=(42, 43, 48, 255))


def _palette(k: _Pen) -> None:
    k.d.ellipse(k.p(3, 4, 29, 28), fill=(220, 190, 150, 255))
    for (x, y), c in zip(((10, 10), (17, 8), (23, 13), (9, 18)), (RED, GREEN, BLUE, YELLOW), strict=True):
        k.d.ellipse(k.p(x - 3, y - 3, x + 3, y + 3), fill=c)
    k.d.ellipse(k.p(17, 18, 24, 25), fill=(42, 43, 48, 255))


def _help(k: _Pen) -> None:
    """An open book."""
    k.d.polygon(k.p(2, 8, 15, 6, 15, 27, 2, 29), fill=BLUE)
    k.d.polygon(k.p(17, 6, 30, 8, 30, 29, 17, 27), fill=(98, 165, 240, 255))
    for y in (12, 16, 20):
        k.d.line(k.p(5, y, 12, y - 1), fill=WHITE, width=max(1, k.w(1.6)))
        k.d.line(k.p(20, y - 1, 27, y), fill=WHITE, width=max(1, k.w(1.6)))


# ----------------------------------------------------------------------------- checkboxes and marks


def _checkbox(k: _Pen) -> None:
    on = k.name == "chk_on"
    k.d.rounded_rectangle(
        k.p(5, 5, 27, 27),
        radius=4 * k.u,
        fill=(47, 116, 208, 255) if on else (24, 25, 28, 255),
        outline=(47, 116, 208, 255) if on else (120, 124, 132, 255),
        width=k.w(2.5),
    )
    if on:
        k.d.line(k.p(10, 16, 14.5, 21, 22.5, 11), fill=WHITE, width=k.w(3.5), joint="curve")


def _mark_ok(k: _Pen) -> None:
    k.d.ellipse(k.p(3, 3, 29, 29), fill=(92, 191, 115, 255))
    k.d.line(k.p(9.5, 16.5, 14, 21, 22.5, 11.5), fill=WHITE, width=k.w(3.5), joint="curve")


def _mark_warn(k: _Pen) -> None:
    k.d.polygon(k.p(16, 3, 30, 28, 2, 28), fill=(230, 162, 60, 255))
    k.d.rectangle(k.p(14.5, 11, 17.5, 20), fill=(30, 30, 30, 255))
    k.d.rectangle(k.p(14.5, 22.5, 17.5, 25.5), fill=(30, 30, 30, 255))


def _mark_err(k: _Pen) -> None:
    k.d.ellipse(k.p(3, 3, 29, 29), fill=(239, 91, 85, 255))
    k.d.line(k.p(11, 11, 21, 21), fill=WHITE, width=k.w(3.5))
    k.d.line(k.p(21, 11, 11, 21), fill=WHITE, width=k.w(3.5))


def _image(k: _Pen) -> None:
    k.d.rounded_rectangle(k.p(3, 6, 29, 26), radius=2 * k.u, outline=(160, 170, 185, 255), width=k.w(2.5))
    k.d.polygon(k.p(6, 23, 13, 14, 18, 20, 21, 17, 26, 23), fill=(92, 191, 115, 255))
    k.d.ellipse(k.p(20, 9, 25, 14), fill=YELLOW)


def _search(k: _Pen) -> None:
    k.d.ellipse(k.p(4, 4, 21, 21), outline=(160, 166, 176, 255), width=k.w(3))
    k.d.line(k.p(18, 18, 28, 28), fill=(160, 166, 176, 255), width=k.w(4))


def _section(k: _Pen) -> None:
    """Section header triangle: pointing down when open, right when closed."""
    if k.name == "sec_open":
        k.d.polygon(k.p(6, 10, 26, 10, 16, 24), fill=LIGHT_GREY)
    else:
        k.d.polygon(k.p(10, 6, 24, 16, 10, 26), fill=LIGHT_GREY)


def _arrow(k: _Pen) -> None:
    """Outline arrows for the item-list toolbar; ``top``/``bottom`` add a bar."""
    if k.name in ("up", "top"):
        k.d.polygon(k.p(16, 6, 28, 24, 4, 24), outline=LIGHT_GREY, width=k.w(2.5))
        if k.name == "top":
            k.d.line(k.p(4, 3, 28, 3), fill=LIGHT_GREY, width=k.w(2.5))
    else:
        k.d.polygon(k.p(16, 26, 28, 8, 4, 8), outline=LIGHT_GREY, width=k.w(2.5))
        if k.name == "bottom":
            k.d.line(k.p(4, 29, 28, 29), fill=LIGHT_GREY, width=k.w(2.5))


_DRAWERS: dict[str, Callable[[_Pen], None]] = {
    "new": _new,
    "open": _open,
    "save": _save,
    "saveas": _save,
    "add": _add,
    "duplicate": _duplicate,
    "delete": _delete,
    "export": _export,
    "folder": _folder,
    "color": _color,
    "extract": _extract,
    "tobj": _tobj,
    "vehicles": _vehicles,
    "cargo": _cargo,
    "preview": _preview,
    "uv": _uv,
    "model": _model,
    "chk_on": _checkbox,
    "chk_off": _checkbox,
    "mark_ok": _mark_ok,
    "mark_warn": _mark_warn,
    "mark_err": _mark_err,
    "image": _image,
    "search": _search,
    "sec_open": _section,
    "sec_closed": _section,
    "gear": _gear,
    "palette": _palette,
    "help": _help,
    "top": _arrow,
    "up": _arrow,
    "down": _arrow,
    "bottom": _arrow,
}


def draw(name: str, size: int = 32) -> Image.Image:
    """Draw one icon.

    Args:
        name: Icon name (see ``_DRAWERS``). An unknown name gives a transparent image.
        size: Final width and height in pixels.

    Returns:
        An RGBA image of ``size`` x ``size``.
    """
    img = _canvas(size)
    drawer = _DRAWERS.get(name)
    if drawer is not None:
        drawer(_Pen(ImageDraw.Draw(img), size * S / 32, name))
    return _done(img, size)


class Icons:
    """Cache of ``PhotoImage`` icons, sized by the current UI scale.

    Keep the instance alive as long as the icons are shown: Tk drops an image as soon as its
    Python object is collected.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, bool], ImageTk.PhotoImage] = {}

    def get(self, name: str, size: int = 32, disabled: bool = False) -> ImageTk.PhotoImage:
        """Return the icon ``name`` at ``size`` logical pixels (scaled by ``theme.SCALE['icons']``).

        Args:
            name: Icon name, as for :func:`draw`.
            size: Size at 100 % UI scale.
            disabled: Grey, one-third-opacity version for disabled ribbon buttons.
        """
        size = max(8, round(size * SCALE.get("icons", 1.0)))
        key = (name, size, disabled)
        if key not in self._cache:
            im = draw(name, size)
            if disabled:
                g = im.convert("LA").convert("RGBA")
                a = im.getchannel("A").point(lambda v: v // 3)
                g.putalpha(a)
                im = g
            self._cache[key] = ImageTk.PhotoImage(im)
        return self._cache[key]


_shared: list[Icons] = []  # the lazily created process-wide cache (a list avoids a ``global`` statement)


def shared() -> Icons:
    """Return the process-wide icon cache used by the widgets (created on first use)."""
    if not _shared:
        _shared.append(Icons())
    return _shared[0]
