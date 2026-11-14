"""Read-only access to SCS HashFS archives (.scs, versions 1 and 2).

Only plain files and directory listings are supported, which is all the
vehicle scanner needs (def/*.sii) and what the "Existing mod" item can repack.
Packed textures (v2 chunk types other than 128/129) are skipped.

Files are found by the CityHash64 of their path (optionally salted), so the
archive itself holds no names; directory listings are separate entries.
Layouts, all little-endian:

* v1 header: ``magic u32, version u16, salt u16, hash_method u32, entry_count u32,
  table_offset u64``; the entry table is ``entry_count`` 32-byte records
  ``hash u64, offset u64, flags u32, crc u32, size u32, compressed_size u32``
  (flag 1 = directory listing, flag 2 = zlib-compressed).
* v2 header: ``magic, version, salt, hash_method, entry_count u32,
  entry_table_size u32, metadata_count u32, metadata_table_size u32,
  entry_table_offset u64, metadata_table_offset u64``; both tables are
  zlib-compressed. Entry records are 16 bytes ``hash u64, meta_index u32,
  meta_count u16, flags u16``; the metadata chunk type sits in the 4th byte of
  the first metadata word.

Format notes from the open documentation in sk-zk/TruckLib.HashFs (GPL) and
SCS's modding wiki; this is an independent implementation. CityHash64 is a
port of Google's CityHash (MIT licence, Copyright (c) 2011 Google, Inc.).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, NamedTuple

HASHFS_MAGIC = 0x23534353  # "SCS#"
# Largest file Paintbooth will unpack from an archive. A mod texture is at most a few hundred MB;
# anything claiming more is damaged, or a decompression bomb.
MAX_ENTRY_BYTES = 1 << 30

MASK = (1 << 64) - 1
K0, K1, K2, K3 = 0xC3A5C85C97CB3127, 0xB492B66FBE98F273, 0x9AE16A3B2F90404F, 0xC949D7C7509E6557


def _rot(v: int, s: int) -> int:
    """Rotate a 64-bit value right by ``s`` bits."""
    return v if s == 0 else ((v >> s) | (v << (64 - s))) & MASK


def _f64(b: bytes, p: int = 0) -> int:
    """Little-endian uint64 at ``p``."""
    return int.from_bytes(b[p : p + 8], "little")


def _f32(b: bytes, p: int = 0) -> int:
    """Little-endian uint32 at ``p``."""
    return int.from_bytes(b[p : p + 4], "little")


def _smix(v: int) -> int:
    """CityHash ShiftMix."""
    return v ^ (v >> 47)


def _h16(u: int, v: int) -> int:
    """CityHash Hash128to64: fold two 64-bit values into one."""
    mul = 0x9DDFEA08EB382D69
    a = ((u ^ v) * mul) & MASK
    a ^= a >> 47
    b = ((v ^ a) * mul) & MASK
    b ^= b >> 47
    return (b * mul) & MASK


def _len0to16(s: bytes, n: int) -> int:
    """CityHash64 of a 0-16 byte input."""
    if n > 8:
        a, b = _f64(s), _f64(s, n - 8)
        return _h16(a, _rot((b + n) & MASK, n)) ^ b
    if n >= 4:
        a = _f32(s)
        return _h16((n + (a << 3)) & MASK, _f32(s, n - 4))
    if n > 0:
        y = s[0] + (s[n >> 1] << 8)
        z = n + (s[n - 1] << 2)
        return (_smix(((y * K2) ^ (z * K3)) & MASK) * K2) & MASK
    return K2


def _len17to32(s: bytes, n: int) -> int:
    """CityHash64 of a 17-32 byte input."""
    a = (_f64(s) * K1) & MASK
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * K2) & MASK
    d = (_f64(s, n - 16) * K0) & MASK
    return _h16((_rot((a - b) & MASK, 43) + _rot(c, 30) + d) & MASK, (a + _rot(b ^ K3, 20) - c + n) & MASK)


def _len33to64(s: bytes, n: int) -> int:
    """CityHash64 of a 33-64 byte input."""
    z = _f64(s, 24)
    a = (_f64(s) + (n + _f64(s, n - 16)) * K0) & MASK
    b = _rot((a + z) & MASK, 52)
    c = _rot(a, 37)
    a = (a + _f64(s, 8)) & MASK
    c = (c + _rot(a, 7)) & MASK
    a = (a + _f64(s, 16)) & MASK
    vf = (a + z) & MASK
    vs = (b + _rot(a, 31) + c) & MASK
    a = (_f64(s, 16) + _f64(s, n - 32)) & MASK
    z = _f64(s, n - 8)
    b = _rot((a + z) & MASK, 52)
    c = _rot(a, 37)
    a = (a + _f64(s, n - 24)) & MASK
    c = (c + _rot(a, 7)) & MASK
    a = (a + _f64(s, n - 16)) & MASK
    wf = (a + z) & MASK
    ws = (b + _rot(a, 31) + c) & MASK
    r = _smix(((vf + ws) * K2 + (wf + vs) * K0) & MASK)
    return (_smix((r * K0 + vs) & MASK) * K2) & MASK


def _weak(w: int, x: int, y: int, z: int, a: int, b: int) -> tuple[int, int]:
    """CityHash WeakHashLen32WithSeeds on four given words."""
    a = (a + w) & MASK
    b = _rot((b + a + z) & MASK, 21)
    c = a
    a = (a + x + y) & MASK
    b = (b + _rot(a, 44)) & MASK
    return (a + z) & MASK, (b + c) & MASK


def _weak_s(s: bytes, p: int, a: int, b: int) -> tuple[int, int]:
    """CityHash WeakHashLen32WithSeeds on the 32 bytes of ``s`` at ``p``."""
    return _weak(_f64(s, p), _f64(s, p + 8), _f64(s, p + 16), _f64(s, p + 24), a, b)


def cityhash64(s: bytes) -> int:
    """CityHash64 (v1.0.3, as SCS uses it) of ``s``.

    Also names generated material files (``/automat/<hh>/<hash>.mat``), so it must stay bit-exact.
    """
    n = len(s)
    if n <= 16:
        return _len0to16(s, n)
    if n <= 32:
        return _len17to32(s, n)
    if n <= 64:
        return _len33to64(s, n)
    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & MASK
    z = _h16((_f64(s, n - 48) + n) & MASK, _f64(s, n - 24))
    v = _weak_s(s, n - 64, n, z)
    w = _weak_s(s, n - 32, (y + K1) & MASK, x)
    x = (x * K1 + _f64(s)) & MASK
    pos = 0
    left = (n - 1) & ~63
    while True:
        x = (_rot((x + y + v[0] + _f64(s, pos + 8)) & MASK, 37) * K1) & MASK
        y = (_rot((y + v[1] + _f64(s, pos + 48)) & MASK, 42) * K1) & MASK
        x ^= w[1]
        y = (y + v[0] + _f64(s, pos + 40)) & MASK
        z = (_rot((z + w[0]) & MASK, 33) * K1) & MASK
        v = _weak_s(s, pos, (v[1] * K1) & MASK, (x + w[0]) & MASK)
        w = _weak_s(s, pos + 32, (z + w[1]) & MASK, (y + _f64(s, pos + 16)) & MASK)
        z, x = x, z
        pos += 64
        left -= 64
        if left == 0:
            break
    return _h16((_h16(v[0], w[0]) + _smix(y) * K1 + z) & MASK, (_h16(v[1], w[1]) + x) & MASK)


class HashFsError(ValueError):
    """The file is not a readable HashFS archive, or an entry in it is damaged."""


class Entry(NamedTuple):
    """One file or directory listing in the archive (a tuple, so ``entry[4]`` still works)."""

    offset: int  # absolute file offset of the (compressed) data
    size: int  # uncompressed size
    csize: int  # stored size
    compressed: bool  # zlib-compressed
    is_dir: bool  # a directory listing, not a file


class HashFs:
    """An open SCS HashFS archive; read plain files and directory listings by path.

    Use it as a context manager, or call :meth:`close`, so the archive is not kept locked on
    Windows::

        with HashFs(path) as fs:
            data = fs.read("def/vehicle/truck/x/data.sii")

    Attributes:
        path: The archive file.
        version: HashFS version (1 or 2).
        salt: Path salt; when non-zero it is prepended (as decimal text) before hashing.
        entries: Path hash -> :class:`Entry`.
    """

    def __init__(self, path: Path):
        """Open ``path`` and read its entry table.

        Raises:
            HashFsError: Not a HashFS archive, an unsupported version, or damaged tables.
            OSError: The file can't be read.
        """
        self.path = Path(path)
        self.f: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - kept open for reads; see close()
        self.entries: dict[int, Entry] = {}
        try:
            self._read_tables()
        except (struct.error, zlib.error, IndexError) as e:
            self.f.close()
            raise HashFsError(f"{self.path.name}: damaged HashFS archive ({e})") from e
        except BaseException:
            self.f.close()
            raise

    def _read_tables(self) -> None:
        """Parse the header and fill :attr:`entries`."""
        head = self.f.read(64)
        if len(head) < 64:
            raise HashFsError(f"{self.path.name} is not a HashFS archive")
        magic, self.version, self.salt = struct.unpack_from("<IHH", head, 0)
        if magic != HASHFS_MAGIC:
            raise HashFsError(f"{self.path.name} is not a HashFS archive")
        if self.version == 1:
            self._read_v1(head)
        elif self.version == 2:
            self._read_v2(head)
        else:
            raise HashFsError(f"HashFS version {self.version} is not supported")

    def _read_v1(self, head: bytes) -> None:
        """Version 1: one uncompressed table of 32-byte records (see the module docstring)."""
        n, start = struct.unpack_from("<IQ", head, 12)
        self.f.seek(start)
        table = self.f.read(n * 32)
        for i in range(n):
            h, off, flags, _crc, size, csize = struct.unpack_from("<QQIIII", table, i * 32)
            if h not in self.entries:  # the first entry for a hash wins, like the game
                self.entries[h] = Entry(off, size, csize, bool(flags & 2), bool(flags & 1))

    def _read_v2(self, head: bytes) -> None:
        """Version 2: compressed entry and metadata tables (see the module docstring)."""
        (_n, et_len, _nmeta, mt_len, et_start, mt_start) = struct.unpack_from("<IIIIQQ", head, 12)
        self.f.seek(et_start)
        et = _inflate(self.f.read(et_len), MAX_ENTRY_BYTES)
        self.f.seek(mt_start)
        mt = _inflate(self.f.read(mt_len), MAX_ENTRY_BYTES)
        for i in range(len(et) // 16):
            h, midx, mcount, _flags = struct.unpack_from("<QIHH", et, i * 16)
            p = midx * 4
            if mcount == 0:
                continue
            chunk = mt[p + 3]
            body = p + 4 * mcount
            if chunk in (128, 129):  # plain file / directory listing
                # sizes are 28-bit little-endian; bit 4 of the 4th compressed-size byte = zlib
                c0, c1, c2, c3, s0, s1, s2, s3, _unk, block = struct.unpack_from("<BBBBBBBBII", mt, body)
                csize = c0 | c1 << 8 | c2 << 16 | (c3 & 0x0F) << 24
                size = s0 | s1 << 8 | s2 << 16 | (s3 & 0x0F) << 24
                # data offsets are stored in 16-byte blocks
                self.entries[h] = Entry(block * 16, size, csize, bool(c3 & 0x10), chunk == 129)

    def close(self) -> None:
        """Close the archive file."""
        self.f.close()

    def __enter__(self) -> HashFs:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _hash(self, path: str) -> int:
        """The entry key of ``path`` (no leading/trailing slash, salt prepended)."""
        path = path.strip("/")
        if self.salt:
            path = str(self.salt) + path
        return cityhash64(path.encode("utf-8"))

    def _read(self, entry: Entry, limit: int = MAX_ENTRY_BYTES) -> bytes:
        """The uncompressed bytes of ``entry``, refusing anything larger than ``limit``."""
        off, size, csize, compressed, _ = entry
        if size > limit:
            raise HashFsError(f"{self.path.name}: an entry of {size} bytes is too large to unpack")
        self.f.seek(off)
        if compressed:
            try:
                return _inflate(self.f.read(csize), limit)
            except zlib.error as e:
                raise HashFsError(f"{self.path.name}: damaged compressed entry ({e})") from e
        return self.f.read(size)

    def read(self, path: str) -> bytes | None:
        """The file at ``path``, or None if there is no such file (or it is a directory).

        Raises:
            HashFsError: The entry is damaged or larger than :data:`MAX_ENTRY_BYTES`.
        """
        e = self.entries.get(self._hash(path))
        if e is None or e.is_dir:
            return None
        return self._read(e)

    def listdir(self, path: str) -> tuple[list[str], list[str]] | None:
        """``(sub-folders, files)`` of the directory ``path``, or None if it has no listing.

        v1 listings are text lines, sub-folders marked with a leading ``*``. v2 listings are
        ``count u32``, ``count`` name lengths (u8), then the names; sub-folders start with ``/``.
        """
        e = self.entries.get(self._hash(path))
        if e is None or not e.is_dir:
            return None
        data = self._read(e)
        dirs: list[str] = []
        files: list[str] = []
        if self.version == 1:
            for line in data.decode("utf-8", "replace").splitlines():
                if line:
                    (dirs.append(line[1:]) if line.startswith("*") else files.append(line))
        else:
            try:
                count = struct.unpack_from("<I", data, 0)[0]
            except struct.error as err:
                raise HashFsError(f"{self.path.name}: damaged directory listing for {path!r}") from err
            lens = data[4 : 4 + count]
            pos = 4 + count
            for ln in lens:
                name = data[pos : pos + ln].decode("utf-8", "replace")
                pos += ln
                (dirs.append(name[1:]) if name.startswith("/") else files.append(name))
        return dirs, files

    def walk(self, top: str = "") -> Iterator[str]:
        """Yield every file path below ``top`` that the directory listings reveal."""
        stack = [top.strip("/")]
        seen: set[str] = set()
        while stack:
            d = stack.pop()
            if d in seen:  # a damaged archive could list a folder inside itself
                continue
            seen.add(d)
            listing = self.listdir(d)
            if listing is None:
                continue
            dirs, files = listing
            for f in files:
                yield f"{d}/{f}" if d else f
            for sub in dirs:
                stack.append(f"{d}/{sub}" if d else sub)


def _inflate(data: bytes, limit: int) -> bytes:
    """zlib-decompress ``data``, refusing output larger than ``limit`` (decompression bombs)."""
    d = zlib.decompressobj()
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise HashFsError(f"compressed data unpacks to more than {limit} bytes")
    if not d.eof:
        raise HashFsError("compressed data is truncated")
    return out
