"""The Office-style ribbon at the top of the main window.

The ribbon has a File button (opens :class:`~.startscreen.FileSidebar`) and one tab, Home, whose
page holds groups of large buttons: Project, Items, Export, Converters, File utilities, Resources
and Program. Groups flow left to right and wrap onto a new row when the window is too narrow for
them (large text or UI scale). The ribbon only calls methods of the :class:`~.main.App`; it holds
no state of its own apart from which tab is shown and whether the ribbon is collapsed.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from functools import partial
from tkinter import ttk
from typing import TYPE_CHECKING

from .widgets import Tooltip

if TYPE_CHECKING:
    from .main import App

#: One ribbon button: label, icon name, command, tooltip, enabled.
ButtonSpec = tuple[str, str, Callable[[], object], str, bool]


class _FlowPage(ttk.Frame):
    """A ribbon page: a frame that remembers its groups (and their separators) for re-flowing."""

    def __init__(self, master: ttk.Frame) -> None:
        super().__init__(master, style="Ribbon.TFrame")
        self.flow: list[tuple[ttk.Frame, ttk.Separator]] = []


class Ribbon(ttk.Frame):
    """The ribbon: tab row (File, Home, collapse button) above the page of button groups."""

    def __init__(self, app: App) -> None:
        super().__init__(app.root)
        self.app = app
        tabs = ttk.Frame(self)
        tabs.pack(fill="x")
        self.tabs_frame = tabs
        self.file_btn = ttk.Button(
            tabs,
            text="File",
            style="File.TButton",
            command=lambda: app.file_sidebar.toggle(),  # noqa: PLW0108  # created later
        )
        self.file_btn.pack(side="left", padx=(0, 2))
        self.tab_btns: dict[str, ttk.Button] = {}
        for name in ("Home",):
            b = ttk.Button(tabs, text=name, style="RibbonTab.TButton", command=partial(self.show, name))
            b.pack(side="left")
            self.tab_btns[name] = b
        self.collapse_btn = ttk.Button(tabs, text="^", width=3, style="Tool.TButton", command=self.toggle)
        self.collapse_btn.pack(side="right")
        Tooltip(self.collapse_btn, "Show / hide the ribbon")
        self.body = ttk.Frame(self, style="Ribbon.TFrame")
        self.body.pack(fill="x")
        self.pages: dict[str, _FlowPage] = {}
        self.body.bind("<Configure>", self._reflow_all)
        self.add_more_btn: ttk.Button | None = None
        self.pages["Home"] = self._home()
        self.current: str | None = None
        self.collapsed = False
        self.show("Home")

    def _group(self, parent: _FlowPage, caption: str, buttons: Sequence[ButtonSpec]) -> dict[str, ttk.Button]:
        """Add a captioned group of large buttons to a page.

        Returns:
            The buttons by label, so callers can keep a reference (e.g. to place a menu below one).
        """
        g = ttk.Frame(parent, style="Ribbon.TFrame")
        row = ttk.Frame(g, style="Ribbon.TFrame")
        row.pack()
        made = {}
        for label, icon, cmd, tip, enabled in buttons:
            b = ttk.Button(
                row,
                text=label,
                image=self.app.icons.get(icon, 32, disabled=not enabled),
                compound="top",
                style="Ribbon.TButton",
                command=cmd,
            )
            if not enabled:
                b.state(["disabled"])
            b.pack(side="left", padx=1)
            Tooltip(b, tip)
            made[label] = b
        ttk.Label(g, text=caption, style="Ribbon.TLabel").pack(pady=(0, 2))
        sep = ttk.Separator(parent, orient="vertical")
        parent.flow.append((g, sep))
        self._reflow(parent)
        return made

    def _home(self) -> _FlowPage:
        """Build the Home page."""
        p = _FlowPage(self.body)
        a = self.app
        self._group(
            p,
            "Project",
            [
                ("New", "new", a.new_dialog, "New project (Ctrl+N)", True),
                ("Open", "open", a.open_dialog, "Open a project, .pb or Mods Studio 2 .ms2 (Ctrl+O)", True),
                ("Save", "save", a.save, "Save (Ctrl+S)", True),
                ("Save as", "saveas", a.save_as, "Save under a new name (Ctrl+Shift+S)", True),
                (
                    "Export to\nmod folder",
                    "export",
                    lambda: a.export(to_mod_folder=True),
                    "Build the mod straight into the game's mod folder (Shift+F5)",
                    True,
                ),
            ],
        )
        items = self._group(
            p,
            "Items",
            [
                (
                    "Mod\nmanifest",
                    "tobj",
                    a.show_manifest,
                    "Mod name, author, icon, description (Ctrl+M)",
                    True,
                ),
                ("Add\nSkin", "add", a.add_item, "Add an Advanced Skin (truck or trailer)", True),
                (
                    "Add\nmore",
                    "add",
                    a.show_add_menu,
                    "Lightbox, windscreen decal, door window, billboard, "
                    "cargo container, company logo, existing mod (uses your Mods Studio 2 templates)",
                    True,
                ),
                ("Duplicate", "duplicate", a.duplicate_item, "Duplicate the selected item (Ctrl+D)", True),
                ("Delete", "delete", a.delete_item, "Delete the selected item (Del)", True),
            ],
        )
        self.add_more_btn = items.get("Add\nmore")
        self._group(
            p,
            "Export",
            [
                ("Export", "export", a.export, "Build the mod as .scs (F5)", True),
                (
                    "To mod\nfolder",
                    "export",
                    lambda: a.export(to_mod_folder=True),
                    "Build straight into the game's mod folder (Shift+F5)",
                    True,
                ),
                ("Show\nfolder", "folder", a.show_export, "Show the exported mod in Explorer", True),
            ],
        )
        self._tools(p)
        self._group(
            p,
            "Program",
            [
                (
                    "Settings",
                    "gear",
                    a.open_settings,
                    "Export folders, creator name, versions, default icon…",
                    True,
                ),
                (
                    "Tutorials",
                    "help",
                    a.open_tutorials,
                    "How to use Paintbooth and make paint job mods (F1)",
                    True,
                ),
            ],
        )
        return p

    def _tools(self, p: _FlowPage) -> None:
        """The former 'SCS Tools' tab, now on Home between Export and Program."""
        a = self.app
        self._group(
            p,
            "Converters",
            [
                (
                    "Color\nconverter",
                    "color",
                    a.open_color_converter,
                    "sRGB <-> SCS linear colour values",
                    True,
                )
            ],
        )
        self._group(
            p,
            "File utilities",
            [("TOBJ\nEditor", "tobj", a.open_tobj_editor, "View and edit .tobj texture objects", True)],
        )
        self._group(
            p,
            "Resources",
            [
                (
                    "Vehicles",
                    "vehicles",
                    a.open_vehicle_manager,
                    "Vehicle library: edit XML, scan the game",
                    True,
                )
            ],
        )

    def _reflow_all(self, e: tk.Event) -> None:
        """Re-flow every page when the ribbon is resized."""
        for p in self.pages.values():
            self._reflow(p, e.width)

    def _reflow(self, page: _FlowPage, width: int | None = None) -> None:
        """Lay the groups out left to right and wrap to a new row when the window is too narrow.

        Args:
            page: The page to lay out.
            width: Available width; defaults to the ribbon body's (or the window's, before the
                body has been drawn).
        """
        width = width or self.body.winfo_width()
        if width <= 1:
            width = self.app.root.winfo_width() or 1400
        x, row, col = 0, 0, 0
        for g, sep in page.flow:
            w = g.winfo_reqwidth() + 10
            if x and x + w > width:
                row, col, x = row + 1, 0, 0
            g.grid(row=row, column=col, padx=(4, 4), pady=(4, 2), sticky="n")
            sep.grid(row=row, column=col + 1, sticky="ns", pady=6)
            col += 2
            x += w + 4

    def show(self, name: str) -> None:
        """Switch to the tab ``name``."""
        if self.current:
            self.pages[self.current].pack_forget()
        self.current = name
        if not self.collapsed:
            self.pages[name].pack(fill="x")
        for n, b in self.tab_btns.items():
            b.configure(style="RibbonTabOn.TButton" if n == name else "RibbonTab.TButton")

    def toggle(self) -> None:
        """Collapse the ribbon to its tab row, or expand it again (remembered in the settings)."""
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.collapse_btn.configure(text="v")
        else:
            self.body.pack(fill="x")
            if self.current:
                self.pages[self.current].pack(fill="x")
            self.collapse_btn.configure(text="^")
        self.app.settings["ribbon_collapsed"] = self.collapsed
