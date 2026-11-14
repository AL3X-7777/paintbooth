r"""Path safety checks for every place Paintbooth turns untrusted text into a file path.

Untrusted names reach the file system from several directions:

* member names inside a template-pack ZIP (``templates.import_pack``),
* member names inside another mod's archive (``ms2tpl.read_archive``, the "Existing mod" item),
* paths written in a project's template build steps (``ms2tpl.Engine``), which travel with a
  shared .pb/.ms2 file,
* mod paths that ``builder.Builder.build_files`` writes into an output folder.

Each of them could otherwise use ``..``, an absolute path, a Windows drive (``C:``) or a UNC share
(``\\host\share``) to read or write outside the folder it belongs to. This module is the single
place that decides what is allowed. Keep it free of imports from the rest of Paintbooth.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["UnsafePathError", "clean_member", "has_traversal", "parts_of", "safe_join"]

# "C:", "c:foo" - a Windows drive, absolute or drive-relative
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


_DEVICE_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


class UnsafePathError(ValueError):
    """A path would leave the folder it is meant to stay in."""


def parts_of(name: str) -> list[str]:
    r"""Split a ``/`` or ``\`` separated path into its parts, dropping empty and ``.`` parts.

    Args:
        name: A relative path as written in an archive or a template.

    Returns:
        The path components, in order. ``..`` is kept so callers can reject it.
    """
    return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]


def has_traversal(name: str) -> bool:
    """Return True if ``name`` climbs out of its folder (``..``) or names a Windows drive.

    This is the lenient check used for paths *inside* a mod, where a leading ``/`` just means the
    mod root and is stripped by the caller.
    """
    parts = parts_of(name)
    return any(p == ".." for p in parts) or bool(parts and _DRIVE_RE.match(parts[0]))


def clean_member(name: str, *, allow_root: bool = False) -> str:
    """Return ``name`` as a safe relative ``/``-separated path, or raise.

    Args:
        name: An untrusted relative path (archive member, template path).
        allow_root: Treat leading separators as "relative to the root" and strip them, the way a
            mod path ``/def/x.sii`` means ``def/x.sii``. Without it an absolute path is rejected.

    Returns:
        The path with empty and ``.`` parts removed, joined with ``/``.

    Raises:
        UnsafePathError: The path is absolute (or UNC) and ``allow_root`` is False, or one of its
            parts is ``..``, contains ``:`` (a drive letter or an NTFS alternate data stream), ends
            in a dot or space, or is a Windows device name such as ``NUL`` or ``COM1.txt``.
    """
    norm = name.replace("\\", "/")
    if norm.startswith("/") and not allow_root:
        raise UnsafePathError(f"absolute path not allowed: {name!r}")
    parts = parts_of(norm)
    for p in parts:
        if p == "..":
            raise UnsafePathError(f"'..' not allowed in {name!r}")
        if ":" in p:
            raise UnsafePathError(f"drive or stream name not allowed in {name!r}")
        if p != p.rstrip(". ") or _DEVICE_RE.match(p):
            # Windows drops trailing dots and spaces and treats CON, NUL, COM1... (with any
            # extension) as devices, so such a name would not be the file it claims to be
            raise UnsafePathError(f"name not usable on Windows in {name!r}")
    return "/".join(parts)


def safe_join(root: Path, rel: str, *, allow_root: bool = False) -> Path:
    """Join an untrusted relative path to ``root`` and make sure the result stays inside it.

    The lexical check of :func:`clean_member` is followed by a check of the resolved path, so a
    symbolic link inside ``root`` that points elsewhere is refused too.

    Args:
        root: The folder the result must stay in.
        rel: The untrusted relative path.
        allow_root: See :func:`clean_member`.

    Returns:
        ``root`` joined with the cleaned path (not resolved, so the caller sees familiar names).

    Raises:
        UnsafePathError: The path would end up outside ``root``.
    """
    clean = clean_member(rel, allow_root=allow_root)
    out = Path(root).joinpath(*clean.split("/")) if clean else Path(root)
    base = Path(root).resolve()
    if not out.resolve().is_relative_to(base):
        raise UnsafePathError(f"{rel!r} points outside {root}")
    return out
