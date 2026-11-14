"""SCS model geometry (.pmg, version 0x15): read pieces, and add new pieces.

A .pmg holds the meshes of a model; its .pmd twin (:mod:`paintbooth.pmd`) holds the material paths.
Paintbooth reads the geometry for two things:

* drawing UV templates: the outlines of every paintable triangle in the paint job's UV space;
* "window and trim films": copies of the glass and gloss-black pieces, laid slightly over the
  originals, that carry the skin's artwork (see :mod:`paintbooth.coach`).

File layout (little-endian), version 0x15 as written by SCS's current tools::

    header (112 bytes)
        u8 version (0x15), char[3] "Pmg"
        u32 piece_count, part_count, bone_count; i32 weight_width; u32 locator_count
        u64 skeleton_hash; f32 center[3], radius; f32 bbox_min[3], bbox_max[3]
        u32 skeleton_offset, parts_offset, locators_offset, pieces_offset,
            string_pool_offset, string_pool_size, vertex_pool_offset, vertex_pool_size,
            index_pool_offset, index_pool_size
    parts      24 bytes each: u64 name token, u32 piece_count, piece_offset (index of the first
               piece), locator_count, locator_offset
    locators, string pool  (copied unchanged)
    pieces     100 bytes each:
        u32 edges (index count), verts, texcoord_mask, texcoord_width; i32 material (index into
        the .pmd material list); f32 center[3], radius, bbox_min[3], bbox_max[3];
        i32 vertex_stride, position_offset, normal_offset, uv_offset, rgba_offset, rgba2_offset,
        tangent_offset, bone_index_offset, bone_weight_offset, index_offset
        (absolute file offsets into the pools; -1 = not present)
    vertex pool   interleaved vertices, one block per piece (``vertex_stride`` bytes each; a UV
                  set is two f32, ``texcoord_width`` sets in a row)
    index pool    u16 triangle indices, one block per piece

``texcoord_mask`` maps texture slots to UV sets, four bits per slot from the lowest; 0xF = unused.

Independent implementation; the format notes come from the open-source ConverterPIX and from
reading the DLC files themselves. Every offset is bounds-checked, so a damaged file raises
:class:`PmgError`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np

VERSION = 0x15
_HEADER = struct.Struct("<4sIIIiIQ4f6f10I")
_PART = struct.Struct("<QIIII")
_PIECE = struct.Struct("<IIIIi10f10i")
#: Vertex layout of pieces Paintbooth adds: position, normal, colour, one UV set.
FILM_STRIDE = 36


class PmgError(ValueError):
    """The file is not a version 0x15 .pmg, or one of its offsets points outside the file."""


@dataclass
class Piece:
    """One mesh of the model, already unpacked.

    Attributes:
        material: Index into the .pmd material list.
        positions: (n, 3) float32 vertex positions in model space (metres; y up, -z forward).
        normals: (n, 3) float32 vertex normals.
        uvs: One (n, 2) float32 array per UV set.
        rgba: (n,) uint32 vertex colours, or None.
        triangles: (m, 3) int vertex indices.
        texcoord_mask: The piece's texture-slot-to-UV-set map (kept for the record).
    """

    material: int
    positions: np.ndarray
    normals: np.ndarray
    uvs: list[np.ndarray]
    rgba: np.ndarray | None
    triangles: np.ndarray
    texcoord_mask: int = 0xFFFFFFF0


@dataclass
class Model:
    """A parsed .pmg: the pieces plus the raw bytes needed to write it back."""

    data: bytes
    header: tuple[Any, ...]  # the unpacked header fields, in file order
    pieces: list[Piece] = field(default_factory=list)


def _check(data: bytes, off: int, size: int, what: str) -> None:
    """Raise :class:`PmgError` unless ``size`` bytes at ``off`` lie inside ``data``."""
    if off < 0 or off + size > len(data):
        raise PmgError(f"{what} at offset {off} is outside the file ({len(data)} bytes)")


def _column(data: bytes, off: int, stride: int, count: int, fmt: str, width: int) -> np.ndarray:
    """Read one interleaved vertex attribute (``width`` values of ``fmt``) for ``count`` vertices."""
    item = np.dtype(fmt).itemsize * width
    _check(data, off, (count - 1) * stride + item if count else 0, "vertex data")
    raw = (
        np.frombuffer(data, dtype=np.uint8, count=(count - 1) * stride + item, offset=off) if count else None
    )
    if raw is None:
        return np.zeros((0, width), dtype=fmt)
    rows = np.lib.stride_tricks.as_strided(raw, shape=(count, item), strides=(stride, 1))
    return rows.copy().view(fmt).reshape(count, width)


def read(data: bytes) -> Model:
    """Parse a version 0x15 .pmg.

    Raises:
        PmgError: Wrong version or signature, or a damaged file.
    """
    _check(data, 0, _HEADER.size, "header")
    h = _HEADER.unpack_from(data, 0)
    if h[0] != bytes([VERSION]) + b"gmP":
        raise PmgError(f"not a version 0x15 .pmg (starts with {data[:4]!r})")
    piece_count, pieces_off = h[1], h[20]
    model = Model(data, h)
    for i in range(piece_count):
        off = pieces_off + i * _PIECE.size
        _check(data, off, _PIECE.size, "piece table")
        w = _PIECE.unpack_from(data, off)
        edges, verts, tmask, twidth, mat = w[:5]
        stride, pos, nrm, uv, rgba = w[15:20]
        index_off = w[24]
        if edges % 3 or stride <= 0:
            raise PmgError(f"piece {i}: bad index count {edges} or stride {stride}")
        _check(data, index_off, edges * 2, f"piece {i} indices")
        tris = np.frombuffer(data, dtype="<u2", count=edges, offset=index_off).astype(np.int64).reshape(-1, 3)
        if len(tris) and tris.max() >= verts:
            raise PmgError(f"piece {i}: an index points past its {verts} vertices")
        uvs = [_column(data, uv + 8 * s, stride, verts, "<f4", 2) for s in range(twidth)] if uv >= 0 else []
        model.pieces.append(
            Piece(
                material=mat,
                positions=_column(data, pos, stride, verts, "<f4", 3),
                normals=_column(data, nrm, stride, verts, "<f4", 3)
                if nrm >= 0
                else np.zeros((verts, 3), "f4"),
                uvs=uvs,
                rgba=_column(data, rgba, stride, verts, "<u4", 1).ravel() if rgba >= 0 else None,
                triangles=tris,
                texcoord_mask=tmask,
            )
        )
    return model


def _piece_record(p: Piece, vert_off: int, index_off: int) -> bytes:
    """The 100-byte table entry of a piece written in the :data:`FILM_STRIDE` layout."""
    pos = p.positions.astype("<f4")
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(pos - center, axis=1).max()) if len(pos) else 0.0
    return _PIECE.pack(
        len(p.triangles) * 3, len(pos), 0xFFFFFFF0, 1, p.material,
        *center.tolist(), radius, *lo.tolist(), *hi.tolist(),
        FILM_STRIDE, vert_off, vert_off + 12, vert_off + 28, vert_off + 24, -1, -1, -1, -1, index_off,
    )  # fmt: skip


def _vertex_block(p: Piece) -> bytes:
    """Interleave a new piece's vertices: position, normal, colour, UV (:data:`FILM_STRIDE` bytes)."""
    n = len(p.positions)
    rec = np.zeros(n, dtype=[("pos", "<f4", 3), ("nrm", "<f4", 3), ("rgba", "<u4"), ("uv", "<f4", 2)])
    rec["pos"], rec["nrm"], rec["uv"] = p.positions, p.normals, p.uvs[0]
    rec["rgba"] = p.rgba if p.rgba is not None else 0xFFFFFFFF
    return rec.tobytes()


def add_pieces(model: Model, new: list[Piece]) -> bytes:
    """Return the .pmg bytes of ``model`` with ``new`` pieces appended to its last part.

    The existing pieces, locators and strings are copied byte for byte. The new pieces' records
    go after the old ones, which moves everything behind the piece table; every absolute offset
    that points there is moved with it.

    Raises:
        PmgError: A new piece has more than 65535 vertices (indices are 16 bit).
    """
    d, h = model.data, list(model.header)
    if not new:
        return d
    for p in new:
        if len(p.positions) > 0xFFFF:
            raise PmgError(f"a new piece has {len(p.positions)} vertices; the format allows 65535")
    piece_count, part_count = h[1], h[2]
    parts_off, pieces_off = h[18], h[20]
    str_off, vp_off, vp_size, ip_off, ip_size = h[21], h[23], h[24], h[25], h[26]
    table_end = pieces_off + piece_count * _PIECE.size
    if table_end != str_off or str_off + h[22] != vp_off or vp_off + vp_size != ip_off:
        raise PmgError("unexpected section order; only the standard layout can be extended")
    shift = len(new) * _PIECE.size

    vblocks = [_vertex_block(p) for p in new]
    iblocks = [p.triangles.astype("<u2").tobytes() for p in new]
    vgrowth = sum(map(len, vblocks))

    out = bytearray(d[:table_end])
    # old piece records: vertex offsets move by `shift`; index offsets also by the new vertices
    for i in range(piece_count):
        w = list(_PIECE.unpack_from(d, pieces_off + i * _PIECE.size))
        w[16:24] = [x + shift if x >= 0 else x for x in w[16:24]]
        w[24] += shift + vgrowth
        _PIECE.pack_into(out, pieces_off + i * _PIECE.size, *w)
    # the last part also owns the new pieces
    if part_count:
        po = parts_off + (part_count - 1) * _PART.size
        token, pc, pfirst, lc, lo = _PART.unpack_from(out, po)
        if pfirst + pc != piece_count:
            raise PmgError("the last part does not end with the last piece")
        _PART.pack_into(out, po, token, pc + len(new), pfirst, lc, lo)

    new_vp_off = vp_off + shift
    new_ip_off = new_vp_off + vp_size + vgrowth
    v_at, i_at = new_vp_off + vp_size, new_ip_off + ip_size
    records = b""
    for p, vb, ib in zip(new, vblocks, iblocks, strict=True):
        records += _piece_record(p, v_at, i_at)
        v_at += len(vb)
        i_at += len(ib)
    out += records
    out += d[str_off:vp_off]  # string pool
    out += d[vp_off:ip_off] + b"".join(vblocks)
    out += d[ip_off : ip_off + ip_size] + b"".join(iblocks)

    h[1] = piece_count + len(new)
    h[21], h[23], h[24] = str_off + shift, new_vp_off, vp_size + vgrowth
    h[25], h[26] = new_ip_off, ip_size + sum(map(len, iblocks))
    _HEADER.pack_into(out, 0, *h)
    return bytes(out)
