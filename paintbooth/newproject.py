"""Create a new, empty project folder for the ``project.xml`` pipeline (``paintbooth new``).

The folder gets a commented ``project.xml`` (read by :mod:`paintbooth.model`), an empty
``vehicles/`` and ``art/`` folder, and ``description.txt``. The GUI's ``.pb``/``.ms2``
projects are made by :mod:`paintbooth.ms2skeleton` instead.

New projects do not set ``compatible``: Paintbooth never writes ``compatible_versions``
into manifest.sii unless the user asks for it.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from xml.sax.saxutils import quoteattr

from .names import TOKEN_MAX

# Attribute values are inserted with quoteattr(), which adds the surrounding quotes itself.
PROJECT_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!-- Paintbooth project. Edit this file at any time - open projects reload it live.
     Paths are relative to this folder. -->
<project>
  <!-- package: 1-12 chars [a-z0-9_], names the mod internally. game: ets2 or ats
       compatible="1.50.*" (comma separated) writes compatible_versions[] into the
       manifest; leave it out to write none. -->
  <mod package={package} title={title} author={author} version="1.0" game={game}
       description="description.txt"/>
  <!-- icon="art/mod_icon.png" on <mod> sets the Mod Manager picture (276x162),
       otherwise the first skin image is used. -->

  <!-- output: .scs/.zip file, or a folder name to write an unpacked mod.
       compression: store | fast | small.  texture_format: DXT5 | DXT1 -->
  <build output={output} compression="fast" texture_format="DXT5" icon_size="256x64"/>

  <!-- Folder(s) with vehicle definition XML. Add/edit files there any time. -->
  <vehicles dir="vehicles"/>

  <!-- A skin: id is 1-12 chars [a-z0-9_]. mode: airbrush | colormask | plain.
       Colours: #rrggbb (converted to game linear values) or r,g,b in 0-1.
       Any other SII attribute: <attr name="..." value="..."/> (written as-is).
  <skin id="my_skin" name="My Skin" price="5000" mode="airbrush" base_color="#ffffff"
        icon="art/my_skin_icon.png">
    <target vehicle="scania.s_2016" image="art/my_skin_scania_s.png"/>
    <target vehicle="scania.s_2016" slot="other_cab" image="art/my_skin_scania_s_cab2.png"/>
    <attr name="base_color_locked" value="false"/>
  </skin>
  -->
</project>
"""


def _ascii(s: str) -> str:
    """Reduce ``s`` to ASCII: accents are dropped (``für`` -> ``fur``), other letters removed."""
    s = s.replace("ß", "ss").replace("Æ", "ae").replace("æ", "ae").replace("Ø", "o").replace("ø", "o")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def safe_package(name: str) -> str:
    """Turn a folder name into a package name: lowercase ``[a-z0-9_]``, at most 12 characters.

    ``"Skins für LKW"`` gives ``"skins_fur_lk"``; a name with nothing usable gives ``"my_skins"``.
    """
    s = _ascii(name).lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", s)[:TOKEN_MAX] or "my_skins"


def create_project(
    d: Path,
    package: str | None = None,
    title: str | None = None,
    author: str | None = None,
    game: str = "ets2",
) -> Path:
    """Create a project in folder ``d`` and return the path of its ``project.xml``.

    Args:
        d: The project folder (created if needed).
        package: The mod's package name; derived from the folder name when None.
        title: The mod's title; the package name when None.
        author: The author shown in the Mod Manager.
        game: ``"ets2"`` or ``"ats"``.

    Raises:
        FileExistsError: ``d`` already has a ``project.xml``.
    """
    d = Path(d)
    if (d / "project.xml").exists():
        raise FileExistsError(f"{d / 'project.xml'} already exists")
    package = package or safe_package(d.name)
    (d / "vehicles").mkdir(parents=True, exist_ok=True)
    (d / "art").mkdir(exist_ok=True)
    text = PROJECT_TEMPLATE.format(
        package=quoteattr(package),
        title=quoteattr(title or package),
        author=quoteattr(author or ""),
        game=quoteattr(game),
        output=quoteattr(f"build/{package}.scs"),
    )
    (d / "project.xml").write_text(text, encoding="utf-8")
    (d / "description.txt").write_text((title or package) + "\n", encoding="utf-8")
    return d / "project.xml"
