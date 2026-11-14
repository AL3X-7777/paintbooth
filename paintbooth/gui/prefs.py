"""Reading and writing the Paintbooth settings file (``~/.paintbooth.json``).

The settings hold the recent projects, export folders, window positions and the auto-version history
(``versions``), so losing them silently is costly. Therefore:

* :func:`save_settings` writes a temporary file and moves it over the old one with ``os.replace``,
  which is atomic, so a crash or power cut mid-write leaves either the old or the new file, never
  half of one. The previous good file is kept as ``.paintbooth.json.bak``.
* :func:`read_settings` never throws the file away. If it can't be parsed, it is renamed to
  ``.paintbooth.json.broken``, the error is logged, the ``.bak`` copy is used if there is one, and
  a message is returned so the window can tell the user.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .. import appdirs

__all__ = ["load_settings", "read_settings", "save_settings", "settings_path"]

log = logging.getLogger(__name__)


def settings_path() -> Path:
    """Return the settings file path (resolved on each call, so tests can point HOME elsewhere)."""
    return appdirs.settings_file()


def _sibling(path: Path, suffix: str) -> Path:
    """``path`` with ``suffix`` appended to its full name (``x.json`` -> ``x.json.bak``)."""
    return path.with_name(path.name + suffix)


def _parse(path: Path) -> dict[str, Any]:
    """Read and parse one settings file.

    Raises:
        OSError: The file can't be read.
        ValueError: The file is not a JSON object (``json.JSONDecodeError`` is a ``ValueError``).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the settings file does not contain a JSON object")
    # paths remembered under an earlier program name now live in Documents\Paintbooth
    fixed: dict[str, Any] = appdirs.fix_paths(data)
    return fixed


def read_settings(path: Path | None = None) -> tuple[dict[str, Any], str | None]:
    """Load the settings, recovering from a damaged file.

    Args:
        path: The settings file; defaults to :func:`settings_path`.

    Returns:
        ``(settings, problem)``. ``problem`` is None normally, or a sentence for the user when the
        file was damaged and had to be set aside (the settings then come from the backup, or are
        empty).
    """
    path = path or settings_path()
    if not path.exists():
        return {}, None
    try:
        return _parse(path), None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        log.error("could not read settings %s: %s", path, e)
        reason = str(e)
    kept = _set_aside(path)
    bak = _sibling(path, ".bak")
    try:
        data = _parse(bak)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        if not isinstance(e, FileNotFoundError):
            log.error("could not read settings backup %s: %s", bak, e)
        return {}, (
            f"Your settings file could not be read ({reason}), so Paintbooth starts with default "
            f"settings.{kept}"
        )
    log.warning("settings restored from %s", bak)
    return data, f"Your settings file could not be read ({reason}), so the last backup was used.{kept}"


def _set_aside(path: Path) -> str:
    """Rename a damaged settings file to ``.broken`` so the next save doesn't overwrite it.

    Returns:
        A sentence for the user saying where the file went (with a leading space), or ``""``.
    """
    broken = _sibling(path, ".broken")
    try:
        os.replace(path, broken)
    except OSError as e:
        log.error("could not set %s aside: %s", path, e)
        return ""
    return f" The damaged file was kept as {broken.name}."


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Load the settings (see :func:`read_settings`), without the message for the user."""
    return read_settings(path)[0]


def save_settings(d: dict[str, Any], path: Path | None = None) -> bool:
    """Write the settings atomically and keep the previous file as a ``.bak``.

    Args:
        d: The settings.
        path: The settings file; defaults to :func:`settings_path`.

    Returns:
        True if the file was written. Failures are logged, not raised: the app keeps running with
        its settings in memory and tries again at the next save.
    """
    path = path or settings_path()
    # The PID keeps two Paintbooth windows saving at the same moment from sharing a temp file.
    tmp = _sibling(path, f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        if path.exists():
            shutil.copyfile(path, _sibling(path, ".bak"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:  # TypeError/ValueError: a value json can't write
        log.error("could not save settings to %s: %s", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            log.debug("could not remove %s", tmp)
        return False
    return True
