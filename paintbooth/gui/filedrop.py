"""Files dragged from Explorer onto the Paintbooth window (Windows only).

Tk has no drag-and-drop of files of its own. On Windows the main window is registered with
``DragAcceptFiles`` and its window procedure is wrapped to catch ``WM_DROPFILES``; the dropped
paths are handed to a callback on the Tk thread. Elsewhere :func:`enable` does nothing and
returns False, and the Browse buttons remain the way to pick files.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

# the wrapped window procedures, kept alive for as long as the windows are
_KEEP: list[Any] = []


def enable(root: tk.Misc, on_files: Callable[[list[Path]], None]) -> bool:
    """Accept files dropped on ``root``'s top-level window; ``on_files`` gets their paths.

    Returns:
        True when drops are enabled (Windows), False otherwise or when the hook failed.
    """
    if sys.platform != "win32":
        return False
    try:
        return _enable_windows(root, on_files)
    except (OSError, AttributeError, tk.TclError) as e:  # never let this stop the app starting
        log.info("drag and drop of files is not available: %s", e)
        return False


def _enable_windows(
    root: tk.Misc, on_files: Callable[[list[Path]], None]
) -> bool:  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)  # type: ignore[attr-defined]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = lresult
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [wintypes.HANDLE]

    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    old = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

    def proc(h: int, msg: int, wp: int, lp: int) -> int:
        if msg == WM_DROPFILES:
            files = []
            for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                n = shell32.DragQueryFileW(wp, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(n)
                shell32.DragQueryFileW(wp, i, buf, n)
                files.append(Path(buf.value))
            shell32.DragFinish(wp)
            root.after_idle(on_files, files)
            return 0
        return int(user32.CallWindowProcW(old, h, msg, wp, lp))

    cb = wndproc(proc)
    _KEEP.append(cb)
    user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
    shell32.DragAcceptFiles(hwnd, True)
    return True
