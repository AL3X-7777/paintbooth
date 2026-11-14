"""SCS model descriptors (.pmd, version 4): read and rewrite the material table.

A .pmd lists, for every *look* of a model, one material path per material slot
(``/automat/xx/<hash>.mat``). The file layout is a 64-byte header of sixteen
uint32 values followed by the sections it points at; the material strings are
the last block, so they can be rebuilt freely:

    version, material_count, look_count, piece_count, variant_count, part_count,
    attribute_count, attribute_values_size, material_block_size,
    look_offset, variant_offset, part_attribute_offset, attribute_value_offset,
    attribute_offset, material_offset, material_data_offset

``look_offset`` holds look_count uint64 look names (SCS tokens, see :func:`token_str`).
``material_offset`` holds look_count * material_count absolute file offsets of
NUL-terminated UTF-8 paths inside the material block.

The model files come from Mods Studio 2 templates and other mods, so every offset is
checked: a damaged file raises :class:`PmdError`, never a raw ``struct.error`` or
``ValueError`` from deep inside the parser.

Independent implementation from the publicly documented format.
"""

from __future__ import annotations

import struct

FIELDS = (
    "version",
    "materials",
    "looks",
    "pieces",
    "variants",
    "parts",
    "attribs",
    "attrib_values_size",
    "material_block_size",
    "look_offset",
    "variant_offset",
    "part_attrib_offset",
    "attrib_value_offset",
    "attrib_offset",
    "material_offset",
    "material_data_offset",
)
# SCS token alphabet: a token is a base-38 number, least significant digit first; digit 0 is unused
_TOKEN = "\0" + "0123456789abcdefghijklmnopqrstuvwxyz_"
_HEADER_SIZE = 64


class PmdError(ValueError):
    """The .pmd file is damaged, truncated or of an unsupported version."""


def token_str(v: int) -> str:
    """Decode an SCS token (a uint64 look or variant name) to its text, e.g. ``default``."""
    s = ""
    while v:
        s += _TOKEN[v % 38]
        v //= 38
    return s


def _unpack(fmt: str, data: bytes, off: int, what: str) -> tuple[int, ...]:
    """``struct.unpack_from`` that raises :class:`PmdError` (naming ``what``) when out of range."""
    if off < 0 or off + struct.calcsize(fmt) > len(data):
        raise PmdError(f"damaged .pmd: {what} at offset {off} lies outside the {len(data)}-byte file")
    return struct.unpack_from(fmt, data, off)


def header(data: bytes) -> dict[str, int]:
    """Read and check the 64-byte header.

    Returns:
        Field name (see :data:`FIELDS`) -> value.

    Raises:
        PmdError: The file is too short, not version 4, or a section offset lies outside the file.
    """
    if len(data) < _HEADER_SIZE:
        raise PmdError("file is too short to be a .pmd")
    h = dict(zip(FIELDS, struct.unpack_from("<16I", data, 0), strict=True))
    if h["version"] != 4:
        raise PmdError(f".pmd version {h['version']} is not supported (only version 4)")
    for name in ("look_offset", "material_offset", "material_data_offset"):
        if h[name] > len(data):
            raise PmdError(f"damaged .pmd: {name} {h[name]} lies outside the {len(data)}-byte file")
    return h


def look_names(data: bytes) -> list[str]:
    """The names of the model's looks, in file order."""
    h = header(data)
    return [
        token_str(_unpack("<Q", data, h["look_offset"] + 8 * i, "look name")[0]) for i in range(h["looks"])
    ]


def _offsets(data: bytes, h: dict[str, int]) -> list[int]:
    """The look_count * material_count string offsets of the material table."""
    n = h["materials"] * h["looks"]
    return list(_unpack(f"<{n}I", data, h["material_offset"], "material table"))


def _cstr(data: bytes, off: int) -> str:
    """The NUL-terminated UTF-8 string at ``off``."""
    end = data.find(b"\0", off) if 0 <= off < len(data) else -1
    if end < 0:
        raise PmdError(f"damaged .pmd: no material path at offset {off}")
    try:
        return data[off:end].decode("utf-8")
    except UnicodeDecodeError as e:
        raise PmdError(f"damaged .pmd: material path at offset {off} is not UTF-8") from e


def material_table(data: bytes) -> list[list[str]]:
    """Return the material paths as ``table[look][slot]``.

    Raises:
        PmdError: The file is damaged.
    """
    h = header(data)
    offs = _offsets(data, h)
    m = h["materials"]
    return [[_cstr(data, offs[li * m + s]) for s in range(m)] for li in range(h["looks"])]


def materials(data: bytes) -> list[str]:
    """Unique material paths, in table order."""
    seen: list[str] = []
    for row in material_table(data):
        for p in row:
            if p not in seen:
                seen.append(p)
    return seen


def rewrite(data: bytes, rename: dict[str, str] | None = None, keep_looks: int | None = None) -> bytes:
    """Return the .pmd with material paths renamed and, optionally, looks folded onto the first ones.

    The structure (look count, names) is unchanged, which keeps the matching .pmc collision file
    valid. The material string block is rebuilt with one string per table entry, like SCS's own
    exporter writes it, and its size field is updated.

    Args:
        data: The original .pmd.
        rename: Old material path -> new path. Paths not listed stay.
        keep_looks: When given, every look after the first ``keep_looks`` points at the materials
            of an earlier look (looks wrap around), so the model never refers to textures that
            were not made.

    Raises:
        PmdError: The file is damaged, or the material strings are not the last block.
    """
    h = header(data)
    table = material_table(data)
    rename = rename or {}
    if keep_looks is not None and 0 < keep_looks < len(table):
        table = [table[i % keep_looks] for i in range(len(table))]
    table = [[rename.get(p, p) for p in row] for row in table]

    block_end = h["material_data_offset"] + h["material_block_size"]
    if block_end != len(data):
        raise PmdError("unexpected .pmd layout (material strings are not the last block)")
    base = h["material_data_offset"]
    if h["material_offset"] + 4 * h["materials"] * h["looks"] > base:
        raise PmdError("unexpected .pmd layout (material table overlaps the material strings)")
    blob = bytearray()
    offs = []
    for row in table:
        for p in row:
            offs.append(base + len(blob))
            blob += p.encode("utf-8") + b"\0"
    out = bytearray(data[:base])
    struct.pack_into(f"<{len(offs)}I", out, h["material_offset"], *offs)
    struct.pack_into("<I", out, 4 * FIELDS.index("material_block_size"), len(blob))
    return bytes(out + blob)
