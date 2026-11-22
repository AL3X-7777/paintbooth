"""Files dragged from Explorer onto the Paintbooth window.

Tk has no drag and drop of files of its own, so this uses *tkdnd*, the Tk extension behind the
``tkinterdnd2`` package (bundled with the Windows installer). The extension is loaded into the
existing Tk and the main window is registered as a drop target for files; Windows passes a drop
on any part of the window up to it. Without the package :func:`enable` does nothing and returns
False, and the Browse buttons remain the way to pick files.

(0.20.0 hooked the window procedure through ``ctypes`` instead, which crashed Paintbooth on a
drop; nothing here touches the window procedure.)
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

DND_FILES = "DND_Files"


def enable(root: tk.Tk, on_files: Callable[[list[Path]], None]) -> bool:
    """Accept files dropped on ``root``; ``on_files`` gets their paths on the Tk thread.

    Returns:
        True when drops are enabled, False when tkdnd is not available here.
    """
    try:
        import importlib

        dnd = importlib.import_module("tkinterdnd2.TkinterDnD")  # optional: bundled on Windows
        dnd._require(root)  # loads the tkdnd package into this Tk
        root.tk.call("tkdnd::drop_target", "register", str(root), DND_FILES)
    except Exception as e:  # noqa: BLE001 - a missing or broken extension must never stop the app
        log.info("drag and drop of files is not available: %s", e)
        return False

    def dropped(data: str) -> str:
        try:
            files = parse(root, data)
        except tk.TclError:
            files = []
        if files:
            root.after_idle(on_files, files)  # after tkdnd has finished the drop
        return "copy"

    root.tk.call("bind", str(root), "<<Drop>>", root.register(dropped) + " %D")
    return True


def parse(root: tk.Misc, data: str) -> list[Path]:
    """The paths in tkdnd's drop data (``{C:/a b/x.scs} C:/y.zip``)."""
    return [Path(p) for p in root.tk.splitlist(data) if p]
