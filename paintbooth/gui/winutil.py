"""Tool windows that reopen where the user left them, with a title bar that matches the theme.

Every secondary window (Settings, Tutorials, Vehicle library, TOBJ editor, colour picker...) is made
with :func:`tool_window`. The window position (and size, for resizable windows) is stored in
``app.settings["windows"][key]`` and written to disk when the window closes, through the app's
``save_settings()`` method. Taking the saver from the app (instead of importing the main module)
keeps this module free of an import cycle with :mod:`.main`.
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from typing import Any

from .theme import C, dark_titlebar

__all__ = ["ToolWindow", "dark_titlebar", "is_dark", "tool_window"]

log = logging.getLogger(__name__)


def is_dark(app: Any) -> bool:
    """Return True if the app's settings ask for the dark theme (the default)."""
    return getattr(app, "settings", {}).get("theme", "dark") == "dark"


def _on_screen(win: tk.Misc, x: int, y: int) -> bool:
    """Return True if a window placed at ``x``, ``y`` would be (mostly) visible.

    A position saved on a second monitor that has since been unplugged would otherwise open the
    window off-screen. The margins leave enough of the title bar visible to drag it back.
    """
    try:
        sw, sh = (
            win.winfo_vrootwidth() or win.winfo_screenwidth(),
            win.winfo_vrootheight() or win.winfo_screenheight(),
        )
        vx, vy = win.winfo_vrootx(), win.winfo_vrooty()
    except tk.TclError:
        return True
    return (
        vx - 50 <= x < vx + max(sw, win.winfo_screenwidth()) - 80
        and vy - 20 <= y < vy + max(sh, win.winfo_screenheight()) - 60
    )


def _saved_geometry(win: tk.Misc, saved: str | None, geometry: str | None, resizable: bool) -> str:
    """Work out the geometry string to open a window with.

    Args:
        win: Any widget, used to query the screen size.
        saved: The stored ``"WxH+X+Y"`` (or ``"+X+Y"`` for fixed-size windows), if any.
        geometry: The caller's default ``"WxH"``, if any.
        resizable: Whether the stored size may be reused (fixed-size windows keep their default).

    Returns:
        A geometry string, or ``""`` to let Tk choose.
    """
    geo = geometry or ""
    if not saved:
        return geo
    try:
        size = saved.partition("+")[0]
        x, y = (int(v) for v in saved.split("+")[1:3])
    except (ValueError, IndexError):
        return geo
    if _on_screen(win, x, y):
        geo = (size if resizable and size else (geometry or "").split("+")[0]) + f"+{x}+{y}"
    return geo


class ToolWindow(tk.Toplevel):
    """A ``Toplevel`` that records its geometry under ``key`` and saves the settings when destroyed.

    Use :func:`tool_window` to create one; it also applies the theme and the stored position.
    """

    def __init__(
        self,
        master: tk.Misc,
        store: dict[str, str],
        key: str,
        resizable: bool,
        save: Callable[[], None] | None,
    ) -> None:
        super().__init__(master)
        self._store, self._key, self._resizable, self._save = store, key, resizable, save

    def _remember(self, _event: object = None) -> None:
        """Store the current geometry (only while the window is shown normally, not minimised)."""
        try:
            if self.winfo_ismapped() and self.state() == "normal":
                g = self.geometry()
                self._store[self._key] = g if self._resizable else "+" + g.split("+", 1)[1]
        except tk.TclError:
            pass  # the window is already being torn down; keep the last stored geometry

    def destroy(self) -> None:
        """Remember the geometry, write the settings file, then destroy the window."""
        self._remember()
        if self._save is not None:
            try:
                self._save()
            except Exception:  # closing a window must never fail because the settings can't be saved
                log.exception("could not save settings when closing %s", self._key)
        super().destroy()


def tool_window(
    app: Any,
    key: str,
    title: str,
    geometry: str | None = None,
    resizable: bool = True,
    transient: bool = True,
) -> ToolWindow:
    """Create a themed tool window that remembers its position (and size, if resizable).

    The window is created hidden, given its title-bar colour and position, then shown, so it never
    flashes in the wrong place or with a white title bar.

    Args:
        app: The main :class:`~paintbooth.gui.main.App` (needs ``root`` and ``settings``; its
            ``save_settings()`` is called when the window closes, if it has one).
        key: Name the geometry is stored under in ``settings["windows"]``.
        title: Window title.
        geometry: Default size ``"WxH"`` for the first opening.
        resizable: False for fixed-size dialogs (only their position is remembered).
        transient: Keep the window above the main window.

    Returns:
        The new window. Calling ``destroy()`` on it saves its geometry.
    """
    store = app.settings.setdefault("windows", {})
    w = ToolWindow(app.root, store, key, resizable, getattr(app, "save_settings", None))
    w.withdraw()
    w.title(title)
    w.configure(background=C["bg"])
    if transient:
        w.transient(app.root)
    if not resizable:
        w.resizable(False, False)
    geo = _saved_geometry(w, store.get(key), geometry, resizable)
    if geo:
        w.geometry(geo)
    dark_titlebar(w, is_dark(app))
    w.bind("<Configure>", lambda e: w._remember() if e.widget is w else None, add="+")
    w.protocol("WM_DELETE_WINDOW", w.destroy)
    w.after_idle(w.deiconify)
    return w
