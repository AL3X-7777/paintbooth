"""Writers for the game file formats Paintbooth produces.

* Binary .tobj (texture object) - layout taken from real game-compiled
  samples and verified working mods (magic 0x70B10A01, 40-byte header,
  then length-prefixed paths).
* Legacy DDS with a full mip chain (DXT5 / DXT1 / uncompressed BGRA), and
  DX10 sRGB BGRA for shop icons.
* SII text units (paint jobs, accessory overrides, manifest) and UI .mat.

Every writer here is deterministic: the golden tests compare exports byte for
byte with real Mods Studio 2 output, so any change to these functions changes
every mod Paintbooth builds.
"""

from __future__ import annotations

import io
import struct
from types import ModuleType

from PIL import Image

# --------------------------------------------------------------------------
# TOBJ
# --------------------------------------------------------------------------

TOBJ_MAGIC = 0x70B10A01


# Header bytes 20..39 for each texture profile. "skin" and "ui" are the
# settings of paint job / shop icon textures in mods verified to load in game;
# "game" is a game-compiled DXT5 texture with mips (repeat->clamp, no bias).
_TOBJ_PROFILES = {
    #        20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39
    "skin": "01 00 02 00 02 00 03 03 03 00 00 00 00 00 00 00 00 01 00 00",
    "ui": "01 00 00 00 02 00 03 03 02 00 02 02 00 01 00 00 00 01 00 00",
    "game": "01 00 00 00 02 00 03 03 03 00 02 02 00 00 00 00 00 01 00 00",
}


def tobj_bytes(texture_path: str, *, ui: bool = False, profile: str | None = None) -> bytes:
    """Return a binary .tobj pointing at ``texture_path`` (absolute game path).

    Layout: magic (4 bytes), 16 zero bytes, the 20 profile bytes (filters, address modes, ...),
    then the path length (u32), a zero u32 and the ASCII path.

    Args:
        texture_path: Game path of the .dds, starting with ``/``.
        ui: Shorthand for ``profile="ui"``.
        profile: "skin" (default), "ui" (uncompressed shop icon, no mips) or "game".

    Raises:
        ValueError: The path is not absolute.
        UnicodeEncodeError: The path is not ASCII (the game can't load it).
    """
    if not texture_path.startswith("/"):
        raise ValueError("tobj texture path must be absolute, e.g. /vehicle/...")
    profile = profile or ("ui" if ui else "skin")
    path = texture_path.encode("ascii")
    hdr = struct.pack("<I", TOBJ_MAGIC) + bytes(16) + bytes.fromhex(_TOBJ_PROFILES[profile])
    return hdr + struct.pack("<II", len(path), 0) + path


def read_tobj_path(data: bytes) -> str:
    """Return the first texture path stored in a binary .tobj.

    Raises:
        ValueError: ``data`` is not a binary .tobj.
    """
    if struct.unpack_from("<I", data, 0)[0] != TOBJ_MAGIC:
        raise ValueError("not a binary tobj")
    length = struct.unpack_from("<I", data, 40)[0]
    return data[48 : 48 + length].decode("ascii")


# --------------------------------------------------------------------------
# DDS
# --------------------------------------------------------------------------

_DDSD_CAPS, _DDSD_HEIGHT, _DDSD_WIDTH = 0x1, 0x2, 0x4
_DDSD_PITCH, _DDSD_PIXELFORMAT = 0x8, 0x1000
_DDSD_MIPMAPCOUNT, _DDSD_LINEARSIZE = 0x20000, 0x80000
_DDPF_ALPHAPIXELS, _DDPF_FOURCC, _DDPF_RGB = 0x1, 0x4, 0x40
_DDSCAPS_COMPLEX, _DDSCAPS_TEXTURE, _DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000


def _mip_chain(img: Image.Image, mips: bool) -> list[Image.Image]:
    """Level 0 plus box-filtered half-size levels down to 1x1 (BGRA and the Pillow fallback)."""
    levels = [img]
    if not mips:
        return levels
    cur = img
    while cur.width > 1 or cur.height > 1:
        w, h = max(1, cur.width // 2), max(1, cur.height // 2)
        # reduce() is a fast exact box filter for the common 2x case.
        if cur.width % 2 == 0 and cur.height % 2 == 0 and cur.width > 1 and cur.height > 1:
            cur = cur.reduce(2)
        else:
            cur = cur.resize((w, h), Image.Resampling.BOX)
        levels.append(cur)
    return levels


_BCN: list[ModuleType | None] = []  # memo for _bcn_module(): [] = not tried yet


def _bcn_module() -> ModuleType | None:
    """Our own DXT encoder (good alpha edges), loaded on first use; None -> Pillow's encoder."""
    if not _BCN:
        try:
            from . import bcn

            _BCN.append(bcn)
        except ImportError:  # pragma: no cover - numpy missing
            _BCN.append(None)
    return _BCN[0]


def _encode_bc(level: Image.Image, fourcc: str) -> bytes:
    """One mip level as raw DXT blocks: our encoder, or Pillow's with its DDS header cut off."""
    bcn = _bcn_module()
    if bcn is not None:
        return bcn.encode_level(level, fourcc)
    buf = io.BytesIO()
    level.save(buf, "DDS", pixel_format=fourcc)
    data = buf.getvalue()
    if data[84:88] == b"DX10":  # pragma: no cover - defensive
        return data[148:]
    return data[128:]


def dds_bytes(img: Image.Image, fmt: str = "DXT5", mips: bool = True) -> bytes:
    """Encode ``img`` as a legacy-header DDS (``DXT5``, ``DXT1`` or ``BGRA``).

    Args:
        img: The image; converted to RGBA.
        fmt: "DXT5", "DXT1" or "BGRA" (any case).
        mips: Write the full mip chain down to 1x1.

    Raises:
        ValueError: ``fmt`` is not supported.
    """
    fmt = fmt.upper()
    img = img.convert("RGBA")
    bcn = _bcn_module() if fmt in ("DXT5", "DXT1") else None
    if bcn is not None:
        img = bcn.bleed_colors(img)
        levels = bcn.mip_chain(img, mips)
    else:
        levels = _mip_chain(img, mips)
    w, h = img.size
    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PIXELFORMAT
    caps = _DDSCAPS_TEXTURE
    if len(levels) > 1:
        flags |= _DDSD_MIPMAPCOUNT
        caps |= _DDSCAPS_COMPLEX | _DDSCAPS_MIPMAP

    if fmt in ("DXT5", "DXT1"):
        block = 16 if fmt == "DXT5" else 8
        flags |= _DDSD_LINEARSIZE
        pitch = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
        pf = struct.pack("<II4sIIIII", 32, _DDPF_FOURCC, fmt.encode(), 0, 0, 0, 0, 0)
        body = b"".join(_encode_bc(lv, fmt) for lv in levels)
    elif fmt == "BGRA":
        flags |= _DDSD_PITCH
        pitch = w * 4
        pf = struct.pack(
            "<II4sIIIII",
            32,
            _DDPF_RGB | _DDPF_ALPHAPIXELS,
            b"\0\0\0\0",
            32,
            0x00FF0000,
            0x0000FF00,
            0x000000FF,
            0xFF000000,
        )
        parts = []
        for lv in levels:
            r, g, b, a = lv.split()
            parts.append(Image.merge("RGBA", (b, g, r, a)).tobytes())
        body = b"".join(parts)
    else:
        raise ValueError(f"unsupported DDS format {fmt!r}")

    # depth=1 matches game-compiled samples.
    header = struct.pack("<4sIIIIIII44x", b"DDS ", 124, flags, h, w, pitch, 1, len(levels))
    header += pf + struct.pack("<IIII4x", caps, 0, 0, 0)
    assert len(header) == 128
    return header + body


def dds_bgra_srgb_dx10(img: Image.Image) -> bytes:
    """Uncompressed B8G8R8A8_UNORM_SRGB with a DX10 header, no mips (shop icons)."""
    img = img.convert("RGBA")
    w, h = img.size
    r, g, b, a = img.split()
    body = Image.merge("RGBA", (b, g, r, a)).tobytes()
    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PITCH | _DDSD_PIXELFORMAT | _DDSD_MIPMAPCOUNT
    header = struct.pack("<4sIIIIIII44x", b"DDS ", 124, flags, h, w, w * 4, 1, 1)
    header += struct.pack("<II4sIIIII", 32, _DDPF_FOURCC, b"DX10", 0, 0, 0, 0, 0)
    header += struct.pack("<IIII4x", _DDSCAPS_TEXTURE, 0, 0, 0)
    header += struct.pack("<IIIII", 91, 3, 0, 1, 1)  # DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, 2D, alpha straight
    return header + body


def is_dds(data: bytes) -> bool:
    """Return True if ``data`` starts with a DDS header (magic plus header size 124)."""
    return len(data) >= 128 and data[:4] == b"DDS " and struct.unpack_from("<I", data, 4)[0] == 124


def dds_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` from a DDS header (stored as height, width at offset 12)."""
    h, w = struct.unpack_from("<II", data, 12)
    return w, h


# --------------------------------------------------------------------------
# SII helpers
# --------------------------------------------------------------------------


def sii_str(value: str) -> str:
    """``value`` as a quoted SII string, with backslashes and quotes escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sii_float3(rgb: tuple[float, float, float]) -> str:
    """An SII float3 like ``(0.5, 1, 0)``: 4 decimals, trailing zeros dropped (as MS2 writes)."""
    return "(" + ", ".join(f"{c:.4f}".rstrip("0").rstrip(".") if c else "0" for c in rgb) + ")"


def sii_file(units: list[tuple[str, str, list[tuple[str, str]]]]) -> str:
    """Render ``[(class, name, [(key, rendered_value), ...]), ...]`` as SII."""
    out = ["SiiNunit", "{"]
    for cls, name, attrs in units:
        out.append(f"{cls} : {name}")
        out.append("{")
        for key, val in attrs:
            out.append(f"\t{key}: {val}")
        out.append("}")
        out.append("")
    if out[-1] == "":
        out.pop()
    out.append("}")
    return "\n".join(out) + "\n"


def ui_mat(tobj_name: str) -> str:
    """A UI material (shop icon) that shows ``tobj_name``, clamped and without mip filtering."""
    return (
        'effect : "ui.rfx" {\n\ttexture : "texture" {\n\t\tsource : '
        + sii_str(tobj_name)
        + "\n\t\tu_address : clamp\n\t\tv_address : clamp\n\t\tmip_filter : none\n\t}\n}\n"
    )
