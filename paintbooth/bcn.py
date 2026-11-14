"""DXT1 / DXT5 (BC1 / BC3) texture compression, done carefully for paint job textures.

Why not Pillow's built-in encoder: it quantises alpha badly (neighbouring alpha values along an
anti-aliased edge collapse to one value per 4x4 block, and fully transparent pixels next to an edge
become partly visible), which shows up as dashed, blocky edges on templates and line art.

What this encoder does:
* Alpha (DXT5): per block, both of the format's alpha modes are tried (8 interpolated values between
  the block's min and max, or 6 values plus exact 0 and 255) and the one with less error is kept;
  every pixel gets its nearest value.
* Colour: endpoints along the block's principal colour axis, fitted only to the pixels you can see
  (weighted by alpha), then each pixel gets its nearest of the 4 palette colours.
* Edges: fully transparent pixels take the colour of the nearest painted pixels ("colour bleeding"),
  so texture filtering and mipmaps don't pull dark or white fringes into partly transparent edges.
* Mipmaps: averaged with alpha weighting (premultiplied), the same reason.

Pure numpy; a 4096x4096 texture with mipmaps takes a few seconds.

Block layouts (little-endian, 4x4 pixels per block, pixel p = row * 4 + column):
* BC1 colour, 8 bytes: two RGB565 endpoints c0, c1, then 16 2-bit indices. With c0 > c1 the
  palette is c0, c1, 2/3 c0 + 1/3 c1, 1/3 c0 + 2/3 c1 (four-colour mode, used here throughout).
* BC3 alpha, 8 bytes in front of the colour block: endpoints a0, a1 (one byte each), then 16
  3-bit indices. a0 > a1 gives 8 interpolated values; a0 <= a1 gives 6 plus 0 and 255.

Threads: :data:`THREADS` limits how many threads one :func:`encode_level` call uses. The builder's
worker processes set it to 1, because they already run one process per core.
"""

from __future__ import annotations

import os
from typing import cast

import numpy as np
from PIL import Image

CHUNK_ROWS = 32  # block rows encoded per step (keeps memory use moderate)
THREADS = max(1, min(8, (os.cpu_count() or 2)))  # threads per encode_level call; see module docstring


# ------------------------------------------------------------------------------------- preparation
def bleed_colors(img: Image.Image) -> Image.Image:
    """Give every fully transparent pixel the colour of nearby visible pixels (RGBA -> RGBA).

    Visible pixels and the alpha channel are unchanged. Pillow averages RGBA with alpha weighting
    when it resizes, so each smaller copy holds the average visible colour of its area; those are
    scaled back up and pasted coarse to fine, so the nearest colour wins.
    """
    alpha = img.getchannel("A")
    # one band, so getextrema() is a (min, max) pair; Pillow's stub also allows a tuple per band
    lo, hi = cast("tuple[int, int]", alpha.getextrema())
    if lo > 0 or hi == 0:
        return img
    w, h = img.size
    # start from the image's mean visible colour, for areas no level reaches
    mean = img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    fill = Image.new("RGB", (w, h), tuple(mean[:3]) if isinstance(mean, tuple) else mean)
    for f in (64, 16, 4):  # coarse to fine: fine wins where it has colour
        sw, sh = max(1, w // f), max(1, h // f)
        small = img.resize((sw, sh), Image.Resampling.BOX)
        up = small.resize((w, h), Image.Resampling.BILINEAR)
        mask = up.getchannel("A").point(lambda v: 255 if v > 0 else 0)
        fill.paste(up.convert("RGB"), (0, 0), mask)
    visible = alpha.point(lambda v: 255 if v > 0 else 0)
    out = fill
    out.paste(img.convert("RGB"), (0, 0), visible)
    out.putalpha(alpha)
    return out


def mip_chain(img: Image.Image, mips: bool = True) -> list[Image.Image]:
    """Level 0 plus half-size levels down to 1x1, averaged with alpha weighting."""
    levels = [img]
    if not mips:
        return levels
    cur = img
    while cur.width > 1 or cur.height > 1:
        w, h = max(1, cur.width // 2), max(1, cur.height // 2)
        # Pillow premultiplies RGBA when resizing, so semi-transparent edges keep their colour
        cur = cur.resize((w, h), Image.Resampling.BOX)
        levels.append(cur)
    return levels


# ------------------------------------------------------------------------------------- blocks
def _blocks(arr: np.ndarray) -> np.ndarray:
    """(H, W, C) -> (H/4 * W/4, 16, C) blocks in row-major order, padding edges by repetition."""
    h, w = arr.shape[:2]
    hp, wp = (h + 3) // 4 * 4, (w + 3) // 4 * 4
    if (h, w) != (hp, wp):
        arr = np.pad(arr, ((0, hp - h), (0, wp - w), (0, 0)), mode="edge")
    c = arr.shape[2]
    return arr.reshape(hp // 4, 4, wp // 4, 4, c).transpose(0, 2, 1, 3, 4).reshape(-1, 16, c)


def _pack_bits(idx: np.ndarray, bits: int) -> np.ndarray:
    """(N, 16) small ints -> (N,) uint64 with pixel p at bit p*bits."""
    shifts = np.arange(16, dtype=np.uint64) * np.uint64(bits)
    return (idx.astype(np.uint64) << shifts).sum(axis=1, dtype=np.uint64)


# ------------------------------------------------------------------------------------- alpha (BC4 style)
def _alpha_palettes(a0: np.ndarray, a1: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The 8-value and 6-value BC3 palettes for endpoints ``a0``/``a1``, each (N, 8) float32.

    The rounding matches the decoders the game uses (integer interpolation, +3 / 7 and +2 / 5).
    """
    a0 = a0.astype(np.float32)[:, None]
    a1 = a1.astype(np.float32)[:, None]
    i = np.arange(1, 7, dtype=np.float32)[None, :]
    p8 = np.concatenate([a0, a1, np.floor(((7 - i) * a0 + i * a1 + 3) / 7)], axis=1)
    j = np.arange(1, 5, dtype=np.float32)[None, :]
    n = a0.shape[0]
    p6 = np.concatenate(
        [
            a0,
            a1,
            np.floor(((5 - j) * a0 + j * a1 + 2) / 5),
            np.zeros((n, 1), np.float32),
            np.full((n, 1), 255, np.float32),
        ],
        axis=1,
    )
    return p8, p6


def _encode_alpha(alpha: np.ndarray) -> np.ndarray:
    """(N, 16) uint8 -> (N, 8) uint8 BC3 alpha blocks."""
    a = alpha.astype(np.int32)
    amax, amin = a.max(axis=1), a.min(axis=1)
    # mode 8: a0 > a1 (max, min)
    e8_0, e8_1 = amax.copy(), amin.copy()
    flat = e8_0 == e8_1  # one alpha value in the whole block
    # mode 6: a0 <= a1, endpoints from values other than 0 and 255
    inner = np.where((a > 0) & (a < 255), a, -1)
    imax = inner.max(axis=1)
    inner_lo = np.where(inner >= 0, inner, 999)
    imin = inner_lo.min(axis=1)
    has_inner = imax >= 0
    e6_0 = np.where(has_inner, imin, 0)
    e6_1 = np.where(has_inner, imax, 0)
    p8, _ = _alpha_palettes(e8_0, e8_1)
    _, p6 = _alpha_palettes(e6_0, e6_1)
    af = a.astype(np.float32)[:, :, None]
    d8 = np.abs(af - p8[:, None, :])
    d6 = np.abs(af - p6[:, None, :])
    i8, i6 = d8.argmin(axis=2), d6.argmin(axis=2)
    err8 = np.take_along_axis(d8, i8[..., None], axis=2)[..., 0].astype(np.float32) ** 2
    err6 = np.take_along_axis(d6, i6[..., None], axis=2)[..., 0].astype(np.float32) ** 2
    use6 = (err6.sum(axis=1) < err8.sum(axis=1)) & ~flat
    # a0 must be > a1 for mode 8; equal endpoints decode as mode 6 with value a0 at index 0 (fine)
    e0 = np.where(use6, e6_0, e8_0).astype(np.uint64)
    e1 = np.where(use6, e6_1, e8_1).astype(np.uint64)
    idx = np.where(use6[:, None], i6, i8)
    idx = np.where(flat[:, None], 0, idx)
    bits = _pack_bits(idx, 3)
    word = e0 | (e1 << np.uint64(8)) | (bits << np.uint64(16))
    return word.astype("<u8").view(np.uint8).reshape(-1, 8)


# ------------------------------------------------------------------------------------- colour (BC1 style)
def _to565(c: np.ndarray) -> np.ndarray:
    """(N, 3) float RGB 0-255 -> (N,) RGB565 values, rounded to nearest."""
    r = np.clip(np.rint(c[:, 0] * 31 / 255), 0, 31).astype(np.int32)
    g = np.clip(np.rint(c[:, 1] * 63 / 255), 0, 63).astype(np.int32)
    b = np.clip(np.rint(c[:, 2] * 31 / 255), 0, 31).astype(np.int32)
    return (r << 11) | (g << 5) | b


def _from565(v: np.ndarray) -> np.ndarray:
    """(N,) RGB565 -> (N, 3) float32 RGB 0-255, expanded by bit replication like the hardware."""
    r = (v >> 11) & 31
    g = (v >> 5) & 63
    b = v & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=1).astype(
        np.float32
    )


def _encode_color(rgb: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """(N, 16, 3) uint8 colours, (N, 16) weights -> (N, 8) uint8 four-colour BC1 blocks."""
    x = rgb.astype(np.float32)
    w = weight.astype(np.float32)
    none = w.sum(axis=1) <= 0
    w = np.where(none[:, None], 1.0, w)  # invisible block: fit all pixels
    ws = w.sum(axis=1, keepdims=True)
    mean = (x * w[..., None]).sum(axis=1) / ws
    d = x - mean[:, None, :]
    cov = np.matmul((d * w[..., None]).transpose(0, 2, 1), d)  # (N, 3, 3)
    # principal axis: power iteration from the block's colour range (converges in a few steps)
    vis = w > 0
    hi = np.where(vis[..., None], x, -1.0).max(axis=1)
    lo = np.where(vis[..., None], x, 999.0).min(axis=1)
    axis = hi - lo + np.float32(1e-3)
    for _ in range(4):
        axis = np.matmul(cov, axis[:, :, None])[:, :, 0]
        axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), np.float32(1e-6))
    t = np.einsum("npc,nc->np", d, axis)
    big = np.float32(1e9)
    tmin = np.where(w > 0, t, big).min(axis=1)
    tmax = np.where(w > 0, t, -big).max(axis=1)
    c_hi = mean + tmax[:, None] * axis
    c_lo = mean + tmin[:, None] * axis
    q0, q1 = _to565(c_hi), _to565(c_lo)
    swap = q0 < q1
    q0, q1 = np.where(swap, q1, q0), np.where(swap, q0, q1)
    p0, p1 = _from565(q0), _from565(q1)
    # nearest palette entry by projecting onto the p0 -> p1 segment (palette: p0, p1, 2/3, 1/3)
    seg = p1 - p0
    den = (seg * seg).sum(axis=1)
    s = np.einsum("npc,nc->np", x - p0[:, None, :], seg) / np.maximum(den, 1e-6)[:, None]
    step = np.clip(np.rint(s * 3), 0, 3).astype(np.int64)  # 0..3 along p0 -> p1
    idx = np.array([0, 2, 3, 1], dtype=np.int64)[step]
    idx = np.where((q0 == q1)[:, None], 0, idx)
    bits = _pack_bits(idx, 2)
    word = q0.astype(np.uint64) | (q1.astype(np.uint64) << np.uint64(16)) | (bits << np.uint64(32))
    return word.astype("<u8").view(np.uint8).reshape(-1, 8)


def _flat_blocks(px: np.ndarray, with_alpha: bool) -> np.ndarray:
    """Blocks whose 16 pixels are all ``px`` (N, 4): exact-as-possible encoding without fitting."""
    n = px.shape[0]
    q = _to565(px[:, :3].astype(np.float32))
    color = (
        (q.astype(np.uint64) | (q.astype(np.uint64) << np.uint64(16)))
        .astype("<u8")
        .view(np.uint8)
        .reshape(n, 8)
    )
    if not with_alpha:
        return color
    a = px[:, 3].astype(np.uint64)
    # a0 = a1 = value with all indices 0 decodes to exactly that value
    alpha = (a | (a << np.uint64(8))).astype("<u8").view(np.uint8).reshape(n, 8)
    return np.concatenate([alpha, color], axis=1)


# ------------------------------------------------------------------------------------- public
def encode_level(img: Image.Image, fourcc: str) -> bytes:
    """Encode one mip level as raw DXT1/DXT5 block data (no DDS header).

    Args:
        img: The level; converted to RGBA.
        fourcc: "DXT5" (16-byte blocks with alpha) or anything else for DXT1 (8-byte blocks).

    Returns:
        The blocks, row by row.
    """
    rgba = np.asarray(img.convert("RGBA"))
    h, w = rgba.shape[:2]
    bw = (w + 3) // 4
    rows = (h + 3) // 4
    starts = list(range(0, rows, CHUNK_ROWS))

    def chunk(r0: int) -> bytes:
        """Encode the block rows ``r0`` .. ``r0 + CHUNK_ROWS``."""
        part = rgba[r0 * 4 : min(h, (r0 + CHUNK_ROWS) * 4)]
        if part.shape[0] % 4:
            part = np.pad(part, ((0, 4 - part.shape[0] % 4), (0, 0), (0, 0)), mode="edge")
        blk = _blocks(part)
        n = blk.shape[0]
        # fast path: every pixel of the block identical (flat colour, or fully transparent)
        same = (blk == blk[:, :1, :]).all(axis=(1, 2))
        if fourcc == "DXT5":
            res = np.empty((n, 16), np.uint8)
            if same.any():
                res[same] = _flat_blocks(blk[same, 0, :], True)
            rest = ~same
            if rest.any():
                b = blk[rest]
                a_ = b[:, :, 3]
                res[rest] = np.concatenate(
                    [_encode_alpha(a_), _encode_color(b[:, :, :3], a_.astype(np.float32) / 255.0)], axis=1
                )
        else:
            res = np.empty((n, 8), np.uint8)
            if same.any():
                res[same] = _flat_blocks(blk[same, 0, :], False)
            rest = ~same
            if rest.any():
                res[rest] = _encode_color(blk[rest][:, :, :3], np.ones((int(rest.sum()), 16), np.float32))
        return res.tobytes()

    if len(starts) > 1 and THREADS > 1:
        # numpy releases the GIL in its heavy loops, so chunks of one texture run on several cores
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(min(THREADS, len(starts))) as ex:
            out = list(ex.map(chunk, starts))
    else:
        out = [chunk(r0) for r0 in starts]
    data = b"".join(out)
    assert len(data) == bw * rows * (16 if fourcc == "DXT5" else 8)
    return data
