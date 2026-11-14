"""Small pure helpers shared by the core modules and the GUI.

Before this module existed, each of these was copied into three to five places. Keep this file free of
imports from the rest of Paintbooth, so every module can use it without import cycles.
"""

from __future__ import annotations

__all__ = ["is_pot", "linear_to_srgb", "srgb_to_linear"]


def is_pot(n: int) -> bool:
    """Return True if ``n`` is a positive power of two.

    ETS2/ATS textures must have power-of-two sides, or the game shows them black or scaled badly.

    Args:
        n: A width or height in pixels.
    """
    return n > 0 and n & (n - 1) == 0


def srgb_to_linear(c: float) -> float:
    """Convert one sRGB channel (0-1) to the linear value the game stores in ``base_color``.

    This is the standard IEC 61966-2-1 transfer curve.
    """
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    """Convert one linear channel to sRGB (0-1). Inverse of :func:`srgb_to_linear`.

    Input outside 0-1 is clamped first, so the result is always a displayable value.
    """
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
