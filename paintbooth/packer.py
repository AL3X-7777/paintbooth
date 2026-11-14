"""Minimal, fast ZIP (.scs) writer.

The standard library's zipfile compresses one member at a time on one core.
Here every member is compressed independently (zlib releases the GIL, so
threads run in parallel), incompressible members are stored instead of
deflated, and compressed members can be cached between builds.

Output is reproducible: every member gets the same fixed DOS timestamp, and no
extra fields or comments are written, so the same input gives the same bytes.
The writer has no ZIP64 support; :func:`write_zip` refuses archives over 4 GB.
"""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_DOS_TIME, _DOS_DATE = 0, (2020 - 1980) << 9 | 1 << 5 | 1  # 2020-01-01 00:00, reproducible builds
# compression setting name -> zlib level (None = store everything)
LEVELS: dict[str, int | None] = {"store": None, "fast": 1, "small": 9}
_FLAG_UTF8 = 0x800  # general purpose bit 11: the name is UTF-8


@dataclass(frozen=True)
class Member:
    """One archive member, ready to be written.

    Attributes:
        crc: CRC-32 of the uncompressed data.
        size: Uncompressed size.
        method: 0 = stored, 8 = deflate.
        data: The compressed (or stored) bytes.
    """

    crc: int
    size: int
    method: int
    data: bytes


def pack_member(data: bytes, level: int | None) -> Member:
    """Compress ``data`` as a raw deflate stream at ``level``, or store it.

    Tiny members (64 bytes or less) and members that deflate saves less than 3 % on (already
    compressed textures, JPEGs) are stored, which makes the game's reads cheaper.
    """
    crc = zlib.crc32(data) & 0xFFFFFFFF
    if level is not None and len(data) > 64:
        c = zlib.compressobj(level, zlib.DEFLATED, -15)
        comp = c.compress(data) + c.flush()
        if len(comp) < len(data) * 0.97:
            return Member(crc, len(data), 8, comp)
    return Member(crc, len(data), 0, data)


def pack_all(datas: list[bytes], level: int | None, workers: int) -> list[Member]:
    """:func:`pack_member` for every item of ``datas``, on ``workers`` threads; order is kept."""
    if level is None or len(datas) < 2:
        return [pack_member(d, level) for d in datas]
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(pack_member, datas, [level] * len(datas)))


def write_zip(path: Path, entries: list[tuple[str, Member]]) -> int:
    """Write ``[(name, Member), ...]`` as a ZIP file, atomically (temporary file, then replace).

    Names are written as given (UTF-8 flagged when not ASCII); callers make sure they are safe
    relative paths.

    Args:
        path: The .scs/.zip file to create or replace.
        entries: The members, in archive order.

    Returns:
        The size of the written file.

    Raises:
        ValueError: The archive would be larger than 4 GB.
        PermissionError: ``path`` is locked (on Windows, by the game).
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            central = []
            offset = 0
            for name, m in entries:
                raw = name.encode("utf-8")
                flags = _FLAG_UTF8 if not raw.isascii() else 0
                if offset > 0xFFFFFFFF or m.size > 0xFFFFFFFF:
                    raise ValueError("mod is larger than 4 GB; split it into several mods")
                # local file header: signature, version needed 2.0, flags, method, time, date,
                # crc, compressed size, size, name length, extra length
                hdr = struct.pack(
                    "<IHHHHHIIIHH",
                    0x04034B50,
                    20,
                    flags,
                    m.method,
                    _DOS_TIME,
                    _DOS_DATE,
                    m.crc,
                    len(m.data),
                    m.size,
                    len(raw),
                    0,
                )
                f.write(hdr)
                f.write(raw)
                f.write(m.data)
                # central directory record: as above plus version made by, comment length,
                # disk number, internal/external attributes and the local header offset
                central.append(
                    struct.pack(
                        "<IHHHHHHIIIHHHHHII",
                        0x02014B50,
                        20,
                        20,
                        flags,
                        m.method,
                        _DOS_TIME,
                        _DOS_DATE,
                        m.crc,
                        len(m.data),
                        m.size,
                        len(raw),
                        0,
                        0,
                        0,
                        0,
                        0,
                        offset,
                    )
                    + raw
                )
                offset += len(hdr) + len(raw) + len(m.data)
            cd = b"".join(central)
            # end of central directory record
            f.write(cd)
            f.write(
                struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cd), offset, 0)
            )
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path.stat().st_size
