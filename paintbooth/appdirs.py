r"""Paintbooth's folders and files on disk, and the default 'paintbooth by AL3X' icons.

The user-visible folder is ``Documents\Paintbooth``::

    Projects\                       project files (.pb; Mods Studio 2 .ms2 also work)
    Vehicles\                       vehicle definition XML (the vehicle library)
    Exports\ATS\, Exports\ETS2\     default export locations
    Generated\                      colour swatches made with "Generate color"
    Defaults\                       default icons
    Templates\ETS2\, Templates\ATS\ paint job templates (imported template packs)

Settings live in ``~/.paintbooth.json`` and the texture cache in ``~/.paintbooth/cache``.

Setting the ``PAINTBOOTH_HOME`` environment variable (or the older ``SKINYARD_HOME``)
moves *everything* into that folder, including the settings file and the cache, and
turns off the migration from the folders of the program's earlier names. The tests use this
to stay away from the real user folders.

Nothing here runs at import time; :func:`migrate` is an explicit start-up step of the GUI.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

APP_NAME = "Paintbooth"
# Earlier names of the program, newest first. Their Documents folders are moved on first start.
OLD_NAMES = ("Paint booth", "Skinyard")
DEFAULT_TEXT = "paintbooth by AL3X"
SKIN_ICON_SIZE = (256, 64)
MOD_ICON_SIZE = (276, 162)


def documents() -> Path:
    """Return the user's Documents folder (the real one on Windows, even when it was moved)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PERSONAL (5) = "My Documents", wherever the user moved it
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # type: ignore[attr-defined]
                return Path(buf.value)
        except (OSError, AttributeError, ImportError) as e:
            log.debug("SHGetFolderPathW failed: %s", e)
        return Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
    d = Path.home() / "Documents"
    return d if d.is_dir() else Path.home()


def custom_home() -> Path | None:
    """Return the folder set with ``PAINTBOOTH_HOME`` (or ``SKINYARD_HOME``), or None."""
    env = os.environ.get("PAINTBOOTH_HOME") or os.environ.get("SKINYARD_HOME")
    return Path(env) if env else None


def home() -> Path:
    r"""Return Paintbooth's folder: ``Documents\Paintbooth``, or the custom home."""
    return custom_home() or documents() / APP_NAME


def old_homes() -> list[Path]:
    r"""Return the folders used under the program's earlier names, newest first.

    These are ``Documents\Paint booth`` and ``Documents\Skinyard``. The list is empty when a custom
    home is set, which turns the migration off.
    """
    if custom_home() is not None:
        return []
    docs = documents()
    return [docs / name for name in OLD_NAMES]


def _merge_folder(old: Path, new: Path) -> None:
    """Move ``old`` to ``new``; if ``new`` exists, move over only the files it does not have.

    Nothing in ``new`` is overwritten. ``old`` is removed once it is empty; a file that exists in
    both places stays in ``old``.
    """
    if not new.exists():
        old.rename(new)
        return
    for src in sorted(old.rglob("*")):
        if src.is_file():
            dst = new / src.relative_to(old)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
    # remove the old tree, deepest folders first, as far as it is empty
    for d in sorted((p for p in old.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):  # not empty: a file that is in both places stays
            d.rmdir()
    with contextlib.suppress(OSError):
        old.rmdir()


def migrate() -> bool:
    r"""Move the folders of the program's earlier names to ``Documents\Paintbooth``.

    ``Documents\Paint booth`` is handled first, so its files win over older ``Documents\Skinyard``
    ones of the same name. The folders inside stay the same.

    Returns:
        True if anything was moved.
    """
    new = home()
    moved = False
    for old in old_homes():
        if not old.is_dir():
            continue
        try:
            _merge_folder(old, new)
            moved = True
        except OSError as e:
            log.warning("could not move %s to %s: %s", old, new, e)
    return moved


def moved_path(text: str) -> str:
    r"""Map a path inside an earlier folder to the same path in ``Documents\Paintbooth``.

    Projects and settings saved before a rename still hold paths such as
    ``Documents\Paint booth\Projects\a.png``. Other paths are returned unchanged.
    """
    if not text:
        return text
    for old in old_homes():
        for o in (str(old), str(old).replace("\\", "/")):
            # the next character must end the folder name: "Paint booth2\x" is another folder
            if text.lower().startswith(o.lower()) and text[len(o) : len(o) + 1] in ("", "\\", "/"):
                return str(home()) + text[len(o) :]
    return text


def fix_paths(v: Any) -> Any:
    """Apply :func:`moved_path` to every string (and dict key) in a JSON value.

    Used on the settings, which remember folders and recent files from before a rename.
    """
    if isinstance(v, str):
        return moved_path(v)
    if isinstance(v, list):
        return [fix_paths(x) for x in v]
    if isinstance(v, dict):
        return {moved_path(k): fix_paths(x) for k, x in v.items()}
    return v


def settings_file() -> Path:
    """Return the settings file, ``~/.paintbooth.json`` (inside the custom home when one is set).

    The first time, ``~/.paintbooth.json`` is copied to it with its paths moved to the new folder.
    """
    custom = custom_home()
    if custom is not None:
        return custom / ".paintbooth.json"
    new = Path.home() / ".paintbooth.json"
    old = Path.home() / ".skinyard.json"
    if not new.exists() and old.exists():
        try:
            data = fix_paths(json.loads(old.read_text(encoding="utf-8")))
            new.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except (OSError, ValueError) as e:
            log.warning("could not copy %s to %s: %s", old, new, e)
    return new


def cache_dir() -> Path:
    """Return the texture cache folder, ``~/.paintbooth/cache`` (inside the custom home when one is set)."""
    return (custom_home() or Path.home()) / ".paintbooth" / "cache"


def projects_dir() -> Path:
    """Return the default folder for project files."""
    return home() / "Projects"


def vehicles_dir() -> Path:
    """Return the vehicle library folder."""
    return home() / "Vehicles"


def exports_dir(game: str) -> Path:
    """Return the default export folder for ``game`` (``"ets2"``; anything else means ATS)."""
    return home() / "Exports" / ("ETS2" if game == "ets2" else "ATS")


def generated_dir() -> Path:
    """Return the folder for generated colour swatches."""
    return home() / "Generated"


def defaults_dir() -> Path:
    """Return the folder holding the default icons."""
    return home() / "Defaults"


def templates_dir(game: str) -> Path:
    """Return the paint job template folder for ``game`` (``"ets2"``; anything else means ATS)."""
    return home() / "Templates" / ("ETS2" if game == "ets2" else "ATS")


def ensure() -> Path:
    """Move the old folder if needed (:func:`migrate`), create every Paintbooth folder, return the home."""
    migrate()
    for d in (
        projects_dir(),
        vehicles_dir(),
        exports_dir("ats"),
        exports_dir("ets2"),
        generated_dir(),
        defaults_dir(),
        templates_dir("ets2"),
        templates_dir("ats"),
    ):
        d.mkdir(parents=True, exist_ok=True)
    return home()


# --------------------------------------------------------------------------
# Default icons
# --------------------------------------------------------------------------


def font_dirs() -> list[Path]:
    """Return the folders searched for fonts: the system's, the user's and ``<home>/Fonts``."""
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    else:
        dirs += [
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
            Path("/usr/share/fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ]
    dirs.append(home() / "Fonts")
    return dirs


def find_font(custom: str | None = None, name: str = "forgore") -> Path | None:
    """The font file to use: a chosen file, else an installed font whose file name contains ``name``."""
    if custom and Path(custom).is_file():
        return Path(custom)
    for d in font_dirs():
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf") and name in p.name.lower():
                return p
    return None


def _fallback_font_path() -> str | None:
    """Return a bold system font for the icons when the "forgore" font is not installed."""
    for cand in ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        for d in font_dirs():
            hits = list(d.rglob(cand)) if d.is_dir() else []
            if hits:
                return str(hits[0])
    return None


def _font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load font ``path`` at ``size`` pixels, falling back to Pillow's built-in font."""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError as e:
            log.debug("cannot load font %s: %s", path, e)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _cap_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> float:
    """Return the height of a capital H in ``font``, at least 1."""
    b = font.getbbox("H", anchor="ls")
    return max(1, b[3] - b[1])


_Glyphs = list[tuple[str, Any]]


def _layout(text: str, path: str | None, size: int) -> tuple[_Glyphs, float, float, float]:
    """Lay out ``text`` with a font per character: digits get a bigger size to match the letters' height.

    Returns:
        (glyphs as (character, font), total width, ascent, descent) in pixels.
    """
    base = _font(path, size)
    cap = _cap_height(base)
    glyphs: _Glyphs = []
    for ch in text:
        f = base
        if ch.isdigit():
            b = base.getbbox(ch, anchor="ls")
            h = b[3] - b[1]
            if 0 < h < cap * 0.95:  # this font draws numbers smaller than capitals
                f = _font(path, max(1, round(size * cap / h)))
        glyphs.append((ch, f))
    width = sum(f.getlength(ch) for ch, f in glyphs)
    asc = (
        max(-f.getbbox(ch or "H", anchor="ls")[1] for ch, f in glyphs if ch.strip()) if text.strip() else cap
    )
    desc = max(f.getbbox(ch or "H", anchor="ls")[3] for ch, f in glyphs if ch.strip()) if text.strip() else 0
    return glyphs, width, asc, desc


def render_icon(
    size: tuple[int, int], text: str = DEFAULT_TEXT, font_path: str | Path | None = None
) -> Image.Image:
    """Draw ``text`` in white, centred and as large as fits, on a black image of ``size``.

    Args:
        size: (width, height) in pixels.
        text: The text to draw.
        font_path: A .ttf/.otf file; a bold system font when None.
    """
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    path = str(font_path) if font_path else _fallback_font_path()
    # largest size that fits with a margin
    # binary search for the largest font size whose layout fits
    lo, hi = 4, h * 2
    best: tuple[_Glyphs, float, float, float] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        glyphs, tw, asc, desc = _layout(text, path, mid)
        if tw <= w * 0.9 and asc + desc <= h * 0.8:
            best = (glyphs, tw, asc, desc)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        best = _layout(text, path, 4)
    glyphs, tw, asc, desc = best
    x = (w - tw) / 2
    baseline = (h - (asc + desc)) / 2 + asc
    for ch, f in glyphs:
        d.text((x, baseline), ch, font=f, fill=(255, 255, 255, 255), anchor="ls")
        x += f.getlength(ch)
    return img


def default_icons(font_setting: str | None = None) -> tuple[Path, Path]:
    """(Re)create the default skin and mod icons in :func:`defaults_dir`.

    Only that folder is created; the rest of the Paintbooth folder is left to :func:`ensure`.

    Args:
        font_setting: The font file chosen in Settings, if any.

    Returns:
        (skin_icon.png, mod_icon.jpg).
    """
    defaults_dir().mkdir(parents=True, exist_ok=True)
    font = find_font(font_setting)
    skin = defaults_dir() / "default_skin_icon.png"
    mod = defaults_dir() / "default_mod_icon.jpg"
    render_icon(SKIN_ICON_SIZE, font_path=font).save(skin)
    render_icon(MOD_ICON_SIZE, font_path=font).convert("RGB").save(mod, quality=95)
    return skin, mod


#: Steam's folder name of each game.
STEAM_FOLDERS = {"ets2": "Euro Truck Simulator 2", "ats": "American Truck Simulator"}


def steam_libraries() -> list[Path]:
    r"""Return the Steam library folders on this PC (Windows), main installation first.

    Steam's own folder comes from the registry (``HKCU\Software\Valve\Steam``, ``SteamPath``);
    the other libraries are listed in its ``steamapps\libraryfolders.vdf``. Anything that can't
    be read is skipped, so the result may be empty.
    """
    roots: list[Path] = []
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:  # type: ignore[attr-defined]
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))  # type: ignore[attr-defined]
        except OSError as e:
            log.debug("no Steam in the registry: %s", e)
        roots.append(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam")
    else:
        roots += [Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]
    libs: list[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = [root] + [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        for lib in found:
            if lib not in libs:
                libs.append(lib)
    return libs


def find_game_dir(game: str) -> Path | None:
    """Return the installed game's folder (contains ``def.scs``), looked for in the Steam libraries."""
    for lib in steam_libraries():
        d = lib / "steamapps" / "common" / STEAM_FOLDERS[game]
        if (d / "def.scs").is_file():
            return d
    return None
