"""Make ``paintbooth.ico`` from the Paintbooth logo (``paintbooth/data/logo.png``).

Called by ``build_installer.py`` (``python make_icon.py OUT.ico``). Writes a multi-size icon
(16-256 px) and a 256 px PNG next to it. Needs Pillow.

The logo is the blackletter "PF" on green drawn by AL3X. Each icon size is scaled from the full
logo on its own, which keeps the small sizes sharper than letting Windows shrink the 256 px one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

LOGO = Path(__file__).resolve().parent.parent / "paintbooth" / "data" / "logo.png"
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def tile(size: int, logo: Path = LOGO) -> Image.Image:
    """Return the logo scaled to ``size`` x ``size`` pixels (RGBA).

    Args:
        size: The side length in pixels.
        logo: The square source image.

    Raises:
        OSError: The logo file is missing or not an image.
    """
    with Image.open(logo) as im:
        return im.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)


def main(argv: list[str]) -> int:
    """Write the .ico given as ``argv[1]`` and a .png beside it."""
    out = Path(argv[1])
    frames = [tile(s) for s in ICON_SIZES]
    # Pillow writes one image per entry of append_images, so every size is the hand-scaled one
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in ICON_SIZES], append_images=frames[:-1])
    frames[-1].save(out.with_suffix(".png"))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
