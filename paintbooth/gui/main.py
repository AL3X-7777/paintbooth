"""Paintbooth main window: a fast, dark, Mods Studio 2-style editor for .pb / .ms2 projects.

Layout: the :class:`~.ribbon.Ribbon` on top, the item list on the left (manifest first, then one
row per item), the editor for the selected item on the right, and a status bar showing the
validation result. The :class:`~.startscreen.StartScreen` covers the editor while no project is
open.

:class:`App` owns the window and the open project (``app.project``). Its work is split into
cohesive helpers, and ``App`` keeps thin methods with the old names because the editors and tool
windows call them:

* :class:`~.project_ctl.ProjectController` (``app.projects``): new, open, save, close, outside changes;
* :class:`~.export_ctl.ExportController` (``app.exports``): export and the texture builder;
* :class:`~.library_ctl.LibraryController` (``app.vehicles``): the vehicle library and scanner.

:func:`main` is the program's entry point. All start-up side effects (moving the old Skinyard
folder, logging, Windows DPI and taskbar settings) happen there, not when this module is imported.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, TextIO

from .. import __version__, appdirs, ms2data, tplbuild, vehlib
from ..ms2 import AdvancedTruckSkin, Ms2Project, TemplateItem
from ..ms2build import Problem, validate
from . import colorpick, prefs, theme
from .editors import ManifestEditor, TruckSkinEditor
from .export_ctl import ExportController
from .icons import Icons
from .library_ctl import LibraryController
from .prefs import load_settings, save_settings
from .project_ctl import GAME_NAMES, ProjectController
from .ribbon import Ribbon
from .startscreen import FileSidebar, StartScreen
from .theme import C, dark_titlebar
from .tpleditor import TemplateEditor
from .widgets import Notice, ThumbCache, Tooltip, UiQueue

# Kept importable from here for older callers.
__all__ = ["GAME_NAMES", "App", "Ribbon", "load_settings", "main", "save_settings"]

log = logging.getLogger(__name__)

#: Delay after the last edit before the project is validated again (typing stays smooth).
VALIDATE_DELAY_MS = 300
#: How often the project file and vehicle library are checked for outside changes.
POLL_MS = 1000
#: The item list can't be dragged narrower than this.
MIN_LIST = 180


class App:
    """The main window and everything the editors share.

    Attributes used by the editors and tool windows (keep them when refactoring): ``root``,
    ``settings``, ``project``, ``icons``, ``ui``, ``thumbs``, ``ribbon``, ``pane``,
    ``truck_editor``, ``manifest_editor``, ``vehicle_manager``, ``library``, ``library_dir``,
    ``library_errors``, and the methods below.
    """

    MIN_LIST = MIN_LIST

    def __init__(self, root: tk.Tk, path: Path | None = None) -> None:
        self.root = root
        self.settings, settings_problem = prefs.read_settings()
        self.dark_var = tk.BooleanVar(value=self.settings.get("theme", "dark") == "dark")
        self._apply_theme()
        self.icons = Icons()
        self.ui = UiQueue(root)
        self.thumbs = ThumbCache(root, self.ui)
        appdirs.ensure()
        self._default_icons: tuple[Path, Path] | None = None
        _configure_tplbuild(self.settings)  # where Mods Studio 2 is installed (ms2_dir)
        colorpick.configure(self)
        self.project: Ms2Project | None = None
        self.selected = "manifest"  # "manifest" or "item::<index>"
        self.problems: list[Problem] = []
        self.shown: tk.Misc | None = None
        self.vehicle_manager: Any = None  # tools.VehicleManager while open
        self.projects = ProjectController(self)
        self.exports = ExportController(self)
        self.vehicles = LibraryController(self)
        self.reload_library()
        self._val_job: str | None = None

        root.title("Paintbooth")
        root.geometry(self.settings.get("geometry", "1400x860"))
        dark_titlebar(root, self.dark_var.get())
        root.minsize(1000, 600)
        self.ribbon = Ribbon(self)
        self.ribbon.pack(fill="x")
        if self.settings.get("ribbon_collapsed"):
            self.ribbon.toggle()
        ttk.Separator(root).pack(fill="x")
        self._build_statusbar()
        self._build_banner()
        self._build_panes()
        self._build_welcome()
        self._bind_shortcuts()

        self.notice = Notice(root)
        from . import filedrop

        self.drops = False
        # once the window exists on screen (tkdnd needs its native window to register it)
        root.after(800, lambda: setattr(self, "drops", filedrop.enable(root, self.on_files_dropped)))
        self.start = StartScreen(self)
        self.file_sidebar = FileSidebar(self)
        if path is not None and Path(path).is_file():
            self.open_project(Path(path))
        else:
            self._show(self.welcome)
            self.root.after(1, self.start.show)
            self.status("Ready")
        if settings_problem:
            self.root.after(500, lambda: messagebox.showwarning("Settings", settings_problem))
        root.after(POLL_MS, self._poll)
        root.after(3000, self._check_updates)
        root.after(1000, self.vehicles.auto_library)
        root.after(1500, self.vehicles.auto_coaches)
        root.after(4000, self.vehicles.auto_glass_templates)
        root.after(150, self._restore_sash)
        self.pane.bind("<ButtonRelease-1>", lambda e: self.root.after(10, self._guard_sash), add="+")
        self.pane.bind("<Configure>", lambda e: self.root.after(10, self._guard_sash), add="+")

    # =============================================================== window construction
    def _apply_theme(self) -> None:
        """Apply the theme, text size and UI scale from the settings."""
        theme.apply(
            self.root,
            "dark" if self.dark_var.get() else "light",
            font_size=self.settings.get("font_size", 9),
            ui_scale=self.settings.get("ui_scale", 100) / 100,
        )

    def _build_statusbar(self) -> None:
        """Status text on the left, validation summary (click for details) on the right."""
        root = self.root
        self.statusbar = ttk.Frame(root, padding=(8, 3))
        self.statusbar.pack(side="bottom", fill="x")
        self.status_lbl = ttk.Label(self.statusbar, text="")
        self.status_lbl.pack(side="left")
        self.problem_lbl = ttk.Label(self.statusbar, text="")
        self.problem_lbl.pack(side="right")
        self.update_lbl = ttk.Label(self.statusbar, text="", style="Link.TLabel", cursor="hand2")
        self.problem_lbl.bind("<Button-1>", lambda e: self.show_problems_dialog())
        ttk.Separator(root).pack(side="bottom", fill="x")

    def _build_banner(self) -> None:
        """The (hidden) 'file changed outside Paintbooth' banner."""
        self.banner_frame = ttk.Frame(self.root, style="Banner.TFrame", padding=6)
        self.banner_lbl = ttk.Label(self.banner_frame, text="", style="Banner.TLabel")
        self.banner_lbl.pack(side="left")
        ttk.Button(self.banner_frame, text="Keep mine", command=lambda: self.banner(None)).pack(side="right")
        ttk.Button(self.banner_frame, text="Reload from disk", command=self._reload_from_disk).pack(
            side="right", padx=6
        )

    def _build_panes(self) -> None:
        """Item list (with its toolbar) on the left, the editors on the right."""
        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True)
        self.pane = pane
        left = ttk.Frame(pane, width=int(self.settings.get("list_width", 340) or 340))
        right = ttk.Frame(pane)
        pane.add(left, weight=0)
        pane.add(right, weight=1)
        tb = ttk.Frame(left, padding=(4, 4))
        tb.pack(fill="x")
        buttons: tuple[tuple[str, Callable[[], object], str], ...] = (
            ("delete", self.delete_item, "Delete"),
            ("duplicate", self.duplicate_item, "Duplicate"),
            ("bottom", lambda: self.move_item("bottom"), "Move to bottom"),
            ("down", lambda: self.move_item("down"), "Move down"),
            ("up", lambda: self.move_item("up"), "Move up"),
            ("top", lambda: self.move_item("top"), "Move to top"),
        )
        for icon, cmd, tip in buttons:
            b = ttk.Button(tb, image=self.icons.get(icon, 16), style="Tool.TButton", command=cmd)
            b.pack(side="right")
            Tooltip(b, tip)
        self.items_tv = ttk.Treeview(left, show="tree", selectmode="browse", style="Items.Treeview")
        self.items_tv.pack(fill="both", expand=True, padx=(4, 0), pady=(0, 4))
        self.items_tv.tag_configure("err", foreground=C["err"])
        self.items_tv.tag_configure("warn", foreground=C["warn"])
        self.items_tv.tag_configure("unsupported", foreground=C["muted"])
        self.items_tv.tag_configure("manifest", font="SkBold")
        self.items_tv.bind("<<TreeviewSelect>>", self._item_selected)
        self.items_tv.bind("<Delete>", lambda e: self.delete_item())

        self.content = ttk.Frame(right, padding=(4, 4, 4, 0))
        self.content.pack(fill="both", expand=True)
        self.manifest_editor = ManifestEditor(self.content, self)
        self.truck_editor = TruckSkinEditor(self.content, self)
        self.template_editor = TemplateEditor(self.content, self)
        self.unsupported = ttk.Frame(self.content, padding=30)
        self.unsupported_lbl = ttk.Label(self.unsupported, text="", wraplength=700, justify="left")
        self.unsupported_lbl.pack(anchor="nw")

    def _build_welcome(self) -> None:
        """The editor-area placeholder shown under the start screen."""
        self.welcome = ttk.Frame(self.content, padding=40)
        ttk.Label(self.welcome, text="Paintbooth", style="Head.TLabel").pack(anchor="w")
        ttk.Label(
            self.welcome,
            text="Open a project (.pb, or a Mods Studio 2 .ms2) or start a new one.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 16))
        row = ttk.Frame(self.welcome)
        row.pack(anchor="w")
        ttk.Button(row, text="Open project…", style="Accent.TButton", command=self.open_dialog).pack(
            side="left"
        )
        ttk.Button(row, text="New ATS project", command=lambda: self.new_project("ats")).pack(
            side="left", padx=6
        )
        ttk.Button(row, text="New ETS2 project", command=lambda: self.new_project("ets2")).pack(side="left")

    def _bind_shortcuts(self) -> None:
        """Window close button and the global keyboard shortcuts."""
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        shortcuts: tuple[tuple[str, Callable[[], object]], ...] = (
            ("<Control-s>", self.save),
            ("<Control-S>", self.save_as),
            ("<Control-Shift-S>", self.save_as),
            ("<Control-o>", self.open_dialog),
            ("<Control-n>", self.new_dialog),
            ("<F5>", self.export),
            ("<Shift-F5>", lambda: self.export(to_mod_folder=True)),
            ("<Control-d>", self.duplicate_item),
            ("<Control-m>", self.show_manifest),
            ("<F1>", self.open_tutorials),
        )
        for seq, fn in shortcuts:
            self.root.bind_all(seq, partial(_run_and_break, fn))

    # =============================================================== helpers
    def _restore_sash(self) -> None:
        """Put the list/editor divider back where the user left it."""
        self.root.update_idletasks()
        try:
            w = int(self.settings.get("list_width", 340))
        except (TypeError, ValueError):
            w = 340
        self.pane.sashpos(0, max(MIN_LIST, w))

    def _guard_sash(self) -> None:
        """Never let the item list be dragged (or restored) to nothing."""
        try:
            if self.pane.winfo_width() > 400 and self.pane.sashpos(0) < MIN_LIST:
                self.pane.sashpos(0, MIN_LIST)
        except tk.TclError:
            pass  # the window is closing

    def show_manifest(self) -> None:
        """Select the Mod manifest (and widen the list if it was collapsed)."""
        if self.pane.sashpos(0) < MIN_LIST:
            self._restore_sash()
        if self.project and self.items_tv.exists("manifest"):
            self.items_tv.selection_set("manifest")
            self.items_tv.see("manifest")

    def status(self, text: str) -> None:
        """Show ``text`` in the status bar."""
        self.status_lbl.configure(text=text)

    def save_settings(self) -> bool:
        """Write the settings file (atomically; see :mod:`.prefs`)."""
        return prefs.save_settings(self.settings)

    def project_dir(self) -> Path | None:
        """The open project's folder (relative image paths start here), or None if unsaved."""
        return self.project.path.parent if self.project and self.project.path else None

    def image_dir(self) -> str:
        """Folder the image Browse buttons start in: the default from Settings, else the last one used."""
        d = self.settings.get("default_image_dir")
        if d and Path(d).is_dir():
            return d
        return self.settings.get("image_dir") or str(appdirs.home())

    def remember_image_dir(self, d: Path) -> None:
        """Remember the folder an image was last picked from."""
        self.settings["image_dir"] = str(d)

    def _icons_ready(self) -> tuple[Path, Path]:
        """The generated default (skin, mod) icons, made again if missing."""
        if self._default_icons is None or not all(p.exists() for p in self._default_icons):
            self._default_icons = appdirs.default_icons(self.settings.get("icon_font") or None)
        return self._default_icons

    def default_skin_icon(self) -> Path:
        """The default 256x64 icon for skins without their own."""
        return self._icons_ready()[0]

    def default_mod_icon(self) -> Path:
        """The default 276x162 mod manager icon."""
        return self._icons_ready()[1]

    def installed_dlcs(self) -> list[tuple[str, str, str]]:
        """``(id, name, note)`` for each ``dlc_*.scs`` in the game folder known for the project's game."""
        game = self.project.game if self.project else "ats"
        gdir = self.settings.get("game_dirs", {}).get(game)
        if not gdir or not Path(gdir).is_dir():
            return []
        return [
            (p.stem, p.stem.replace("dlc_", "").replace("_", " ").title(), "Installed DLC")
            for p in sorted(Path(gdir).glob("dlc_*.scs"))
        ]

    def _show(self, frame: tk.Misc) -> None:
        """Show one editor (or placeholder) in the right pane."""
        if self.shown is not frame:
            if self.shown is not None:
                self.shown.pack_forget()  # type: ignore[attr-defined]  # every shown frame is packed
            frame.pack(fill="both", expand=True)  # type: ignore[attr-defined]
            self.shown = frame

    def update_title(self) -> None:
        """Window title: file name, ``*`` when unsaved, project title."""
        if not self.project:
            self.root.title("Paintbooth")
            return
        name = self.project.path.name if self.project.path else "Untitled.pb"
        star = "*" if self.project.dirty else ""
        self.root.title(f"{name}{star}  -  {self.project.title}  -  Paintbooth")

    def banner(self, text: str | None) -> None:
        """Show the banner above the panes with ``text``, or hide it when ``text`` is None."""
        if text:
            self.banner_lbl.configure(text=text)
            self.banner_frame.pack(fill="x", before=self.pane)
        else:
            self.banner_frame.pack_forget()

    _banner = banner  # old name

    # =============================================================== settings
    def open_settings(self) -> None:
        """Open the Settings window."""
        from .settings import SettingsWindow  # imports psdmaker; only needed when opened

        SettingsWindow(self)

    def settings_changed(self, font_changed: bool, look_changed: bool = False) -> None:
        """Apply new settings after the Settings window saved them.

        Args:
            font_changed: The default-icon font changed: regenerate the default icons.
            look_changed: The text size or UI scale changed: re-theme and offer a restart.
        """
        if look_changed:
            self._apply_theme()
            self.save_settings()
            if messagebox.askyesno(
                "Text size and scale",
                "The new text size is applied. Icons and the layout "
                "update fully after a restart.\n\nRestart Paintbooth now?",
            ):
                self.root.after(50, self.restart)
        if font_changed:
            self._default_icons = appdirs.default_icons(self.settings.get("icon_font") or None)
            self.thumbs.cache.clear()
        self.save_settings()
        self.manifest_editor._build()
        if self.project:
            self.manifest_editor.load(self.project.manifest)
        self._load_selected()
        self.status("Settings saved")

    # =============================================================== library (see library_ctl)
    @property
    def library(self) -> vehlib.Library:
        """The loaded vehicle library."""
        return self.vehicles.library

    @property
    def library_dir(self) -> Path:
        """The vehicle library folder."""
        return self.vehicles.library_dir

    @property
    def library_errors(self) -> list[str]:
        """Messages for library files that could not be read."""
        return self.vehicles.library_errors

    def reload_library(self) -> list[str]:
        """Reload the vehicle library; returns the load errors."""
        return self.vehicles.reload()

    def vehicle_catalog(self) -> dict[str, tuple[vehlib.Vehicle, str]]:
        """Library plus project-stored vehicles (see :meth:`.LibraryController.vehicle_catalog`)."""
        return self.vehicles.vehicle_catalog()

    def catalog_key(self, v: vehlib.Vehicle | None) -> str | None:
        """Catalogue key of an embedded vehicle definition."""
        return self.vehicles.catalog_key(v)

    def install_library(self) -> None:
        """Install the vehicle library (button in the Vehicles window)."""
        self.vehicles.install_library()

    def scan_dialog(self, folder: bool = True) -> None:
        """Scan the game (or a mod) for trucks."""
        self.vehicles.scan_dialog(folder)

    # =============================================================== project (see project_ctl)
    def _project_key(self) -> str:
        return self.projects.key()

    def _confirm_discard(self) -> bool:
        return self.projects.confirm_discard()

    def new_dialog(self) -> None:
        """New project for the current game."""
        self.projects.new_dialog()

    def new_project(self, game: str = "ats") -> None:
        """Start a new project for ``game`` (``"ats"`` or ``"ets2"``)."""
        self.projects.new_project(game)

    def open_dialog(self) -> None:
        """Ask for a project and open it."""
        self.projects.open_dialog()

    def open_project(self, path: Path) -> None:
        """Open the project at ``path``."""
        self.projects.open_project(path)

    def save(self) -> bool:
        """Save the project; returns True if written."""
        return self.projects.save()

    def save_as(self) -> bool:
        """Save under a new name; returns True if written."""
        return self.projects.save_as()

    def close_project(self) -> None:
        """Close the project and show the start screen."""
        self.projects.close_project()

    def _reload_from_disk(self) -> None:
        self.projects.reload_from_disk()

    # =============================================================== export (see export_ctl)
    def mod_folder(self, game: str) -> Path:
        """The game's mod folder."""
        return self.exports.mod_folder(game)

    def export_folder(self, game: str) -> Path:
        """The default export folder for ``game``."""
        return self.exports.export_folder(game)

    def next_export_version(self) -> str:
        """The version the next automatic-version export gets."""
        return self.exports.next_export_version()

    def export(self, ask: bool = False, to_mod_folder: bool = False) -> None:
        """Export the mod (see :meth:`.ExportController.export`)."""
        self.exports.export(ask=ask, to_mod_folder=to_mod_folder)

    def show_export(self) -> None:
        """Show the last exported mod in the file manager."""
        self.exports.show_export()

    @property
    def builder(self) -> Any:
        """The texture builder (owned by the export controller)."""
        return self.exports.builder

    @property
    def exporting(self) -> bool:
        """True while an export runs."""
        return self.exports.exporting

    # =============================================================== items
    def refresh_items(self) -> None:
        """Rebuild the item list from the project and show the selected item."""
        tv = self.items_tv
        tv.delete(*tv.get_children())
        if not self.project:
            self._show(self.welcome)
            self.update_title()
            return
        tv.insert("", "end", iid="manifest", text="Mod manifest", tags=("manifest",))
        for i, it in enumerate(self.project.items):
            tags = () if isinstance(it, (AdvancedTruckSkin, TemplateItem)) else ("unsupported",)
            tv.insert("", "end", iid=f"item::{i}", text=it.display(), tags=tags)
        if not tv.exists(self.selected):
            self.selected = "manifest"
        tv.selection_set(self.selected)
        tv.see(self.selected)
        self._load_selected()
        self.update_title()
        self.revalidate_soon()

    def _item_selected(self, _event: object = None) -> None:
        """The user picked another row in the item list."""
        sel = self.items_tv.selection()
        if sel and sel[0] != self.selected:
            self.selected = sel[0]
            self._load_selected()

    def _load_selected(self) -> None:
        """Load the selected item into its editor."""
        if not self.project:
            return
        if self.selected == "manifest":
            self.manifest_editor.load(self.project.manifest)
            self._show(self.manifest_editor)
        else:
            it = self.project.items[int(self.selected.split("::")[1])]
            if isinstance(it, TemplateItem):
                self.template_editor.load(it)
                self._show(self.template_editor)
            elif isinstance(it, AdvancedTruckSkin):
                self.truck_editor.load(it)
                self._show(self.truck_editor)
                if not it.truck_id:
                    self.truck_editor.select(0)
            else:
                self.unsupported_lbl.configure(
                    text=f"'{it.template_name}' items are not supported by Paintbooth yet.\n\n"
                    "They stay in the project file unchanged, so Mods Studio 2 can still edit them, "
                    "but they are skipped when Paintbooth exports the mod."
                )
                self._show(self.unsupported)
        self.show_problems()

    def _index(self) -> int | None:
        """Index of the selected item, or None when the manifest is selected."""
        return int(self.selected.split("::")[1]) if self.selected.startswith("item::") else None

    def _ensure_project(self) -> Ms2Project:
        """The open project; an ATS project is started when none is open."""
        if not self.project:
            self.new_project("ats")
        assert self.project is not None  # new_project only returns early when one is open
        return self.project

    def add_item(self) -> None:
        """Add an Advanced Skin, seeded from a recent project's item when this project has none."""
        project = self._ensure_project()
        seed = None
        if not any(isinstance(i, AdvancedTruckSkin) for i in project.items):
            seed = self.projects.seed_from_recent()
        i = project.add_like("AdvancedTruckSkin", seed)
        self.selected = f"item::{i}"
        self.changed(structure=True)
        self.truck_editor.select(0)
        self.status("Added an Advanced Skin - pick the truck or trailer, then add images on the Skins tab")

    def _seed_from_recent(self) -> Any:
        return self.projects.seed_from_recent()

    # ---- Mods Studio 2 template items
    def ms2_dir(self, ask: bool = True) -> Path | None:
        """The Mods Studio 2 folder; with ``ask``, let the user choose it when it isn't found."""
        d = tplbuild.ms2_dir(self.project) if self.project else ms2data.find(self.settings)
        if d is None and ask:
            if not messagebox.askokcancel(
                "Mods Studio 2 folder",
                "These items are built from Mods Studio 2's own template files (models, materials and "
                "PNG templates), so Mods Studio 2 needs to be installed on this PC.\n\n"
                "Paintbooth didn't find it in the usual places. Choose its folder (the one with "
                "ModsStudio2UI.exe and a Data folder)?",
            ):
                return None
            p = filedialog.askdirectory(title="Mods Studio 2 folder", mustexist=True)
            if not p:
                return None
            if not ms2data.is_ms2_dir(Path(p)):
                messagebox.showerror(
                    "Mods Studio 2 folder",
                    f"{p} has no Data\\ModProjects folder - that isn't a Mods Studio 2 installation.",
                )
                return None
            self.settings["ms2_dir"] = str(Path(p))
            self.save_settings()
            d = Path(p)
        return d

    def show_add_menu(self) -> None:
        """The 'Add more' list: every Mods Studio 2 template kind, greyed out when not for this game.

        It opens when the pointer rests on the button (or on a click). Entries can be dragged onto
        the ribbon to get a button of their own (see :class:`~.ribbon.Ribbon`).
        """
        game = self.project.game if self.project is not None else "ets2"
        ms2 = self.ms2_dir(ask=False)
        have = ms2data.templates(ms2, game) if ms2 else {}
        entries: list[tuple[str, str, Callable[[], object] | None]] = [
            ("Advanced Skin (truck or trailer)", "add_skin", self.add_item),
            ("", "", None),
        ]
        for kind, (label, _help) in ms2data.FEATURES.items():
            if ms2 is not None and kind not in have:
                entries.append((f"{label}  (not for {'ETS2' if game == 'ets2' else 'ATS'})", "", None))
            else:
                entries.append((label, f"add:{kind}", partial(self.add_template, kind)))
        if ms2 is None:
            entries += [
                ("", "", None),
                ("These use Mods Studio 2's templates - set its folder…", "", lambda: self.ms2_dir(ask=True)),
            ]
        self.ribbon.open_add_popup(entries)

    def add_template(self, kind: str) -> None:
        """Add a Mods Studio 2 template item (lightbox, billboard...) after the selected item."""
        project = self._ensure_project()
        ms2 = self.ms2_dir(ask=True)
        if ms2 is None:
            return
        tdef = ms2data.template(ms2, project.game, kind)
        if tdef is None:
            label = ms2data.FEATURES.get(kind, (kind, ""))[0]
            messagebox.showinfo(
                "Add", f"Your Mods Studio 2 has no '{label}' template for {GAME_NAMES[project.game]}."
            )
            return
        cur = self._index()
        i = project.add_template(tdef.root, ms2, None if cur is None else cur + 1)
        self.selected = f"item::{i}"
        self.changed(structure=True)
        self.status(f"Added {ms2data.FEATURES.get(kind, (tdef.name,))[0]} - fill in the fields on the right")

    def on_files_dropped(self, files: list[Path]) -> None:
        """Files dragged from Explorer onto the window.

        Mods (.scs, .zip) go into the open "Existing mod" item's file field, or each becomes a
        new "Existing mod" item. A project file (.pb, .ms2) is opened.
        """
        mods = [f for f in files if f.suffix.lower() in (".scs", ".zip")]
        projects = [f for f in files if f.suffix.lower() in (".pb", ".ms2")]
        if projects and not mods:
            self.open_project(projects[0])
            return
        if not mods:
            self.status(
                "Drag mods (.scs or .zip) here to pack them into the project, or a project to open it"
            )
            return
        if self.shown is self.template_editor and self.template_editor.drop_file(mods[0]):
            self.status(f"Existing mod: {mods[0].name}")
            mods = mods[1:]
        for f in mods:
            self.add_existing_mod(f)

    def add_existing_mod(self, path: Path) -> None:
        """Add an "Existing mod" item that packs the mod at ``path``."""
        if self.project is None:
            self._ensure_project()
        before = len(self.project.items) if self.project else 0
        self.add_template("ExistingMod")
        project = self.project
        if project is None or len(project.items) == before:
            return  # Mods Studio 2 or its template is missing (the user was told)
        it = project.items[self._index() or 0]
        if isinstance(it, TemplateItem):
            for v in it.variables():
                if v.type() == "FileSourceVariable":
                    from ..ms2 import var_set

                    var_set(v, str(path))
            self.changed(structure=True)
            self.status(f"Added the mod {path.name}")

    def duplicate_item(self) -> None:
        """Duplicate the selected item (with a new internal name)."""
        i = self._index()
        if self.project is None or i is None:
            return
        j = self.project.duplicate(i)
        self.selected = f"item::{j}"
        self.changed(structure=True)

    def delete_item(self) -> None:
        """Delete the selected item after asking."""
        i = self._index()
        if self.project is None or i is None:
            return
        it = self.project.items[i]
        if not messagebox.askyesno("Delete", f"Delete '{it.display()}'?"):
            return
        self.project.delete(i)
        n = len(self.project.items)
        self.selected = f"item::{min(i, n - 1)}" if n else "manifest"
        self.changed(structure=True)

    def move_item(self, where: str) -> None:
        """Move the selected item ``"top"``, ``"up"``, ``"down"`` or to the ``"bottom"``."""
        i = self._index()
        if self.project is None or i is None:
            return
        n = len(self.project.items)
        to = {"top": 0, "up": i - 1, "down": i + 1, "bottom": n - 1}[where]
        j = self.project.move(i, to)
        self.selected = f"item::{j}"
        self.changed(structure=True)

    # =============================================================== changes and validation
    def changed(self, label: bool = False, structure: bool = False) -> None:
        """Editors call this after every edit.

        Args:
            label: The selected item's display name may have changed: update its row.
            structure: Items were added, removed or reordered: rebuild the list.
        """
        if structure:
            self.refresh_items()
            return
        i = self._index()
        if label and self.project and i is not None:
            self.items_tv.item(self.selected, text=self.project.items[i].display())
        self.update_title()
        self.revalidate_soon()

    def revalidate_soon(self) -> None:
        """Validate once the user pauses (debounced, so typing never waits for validation)."""
        if self._val_job:
            self.root.after_cancel(self._val_job)
        self._val_job = self.root.after(VALIDATE_DELAY_MS, self.revalidate)

    def revalidate(self) -> None:
        """Validate now: mark items with errors and update the status bar summary."""
        self._val_job = None
        if not self.project:
            return
        self.problems = validate(self.project)
        for i in range(len(self.project.items)):
            iid = f"item::{i}"
            if not self.items_tv.exists(iid):
                continue
            tags = [t for t in self.items_tv.item(iid, "tags") if t not in ("err", "warn")]
            if any(p.level == "error" for p in self.problems if p.item == i):
                tags.append("err")
            self.items_tv.item(iid, tags=tags)
        self.show_problems()
        self._show_problem_summary()

    def _show_problem_summary(self) -> None:
        """The coloured 'N error(s), M warning(s)' / 'Ready to export' label in the status bar."""
        errs = sum(1 for p in self.problems if p.level == "error")
        warns = len(self.problems) - errs
        if errs:
            text, style, icon = (
                f" {errs} error(s), {warns} warning(s) - click for details",
                "Err.TLabel",
                "mark_err",
            )
        elif warns:
            text, style, icon = f" {warns} warning(s) - click for details", "Warn.TLabel", "mark_warn"
        else:
            text, style, icon = " Ready to export", "Ok.TLabel", "mark_ok"
        self.problem_lbl.configure(text=text, style=style, image=self.icons.get(icon, 14), compound="left")

    def show_problems(self) -> None:
        """Pass the selected item's problems to its editor (marks next to the fields)."""
        if self.selected == "manifest":
            self.manifest_editor.show_problems([p for p in self.problems if p.item == -1])
        elif self.shown is self.truck_editor:
            self.truck_editor.show_problems([p for p in self.problems if p.item == self._index()])
        elif self.shown is self.template_editor:
            self.template_editor.show_problems([p for p in self.problems if p.item == self._index()])

    def show_problems_dialog(self) -> None:
        """List every problem in a message box (at most 40 lines)."""
        if not self.problems or not self.project:
            return
        items = self.project.items
        lines = []
        for p in self.problems:
            where = "Mod manifest" if p.item < 0 else items[p.item].display()
            lines.append(f"{'ERROR' if p.level == 'error' else 'Warning'} - {where}:  {p.message}")
        self.alert("Problems", "\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else ""))

    def alert(self, title: str, message: str, error: bool = False) -> None:
        """A message box in front of the Paintbooth window.

        Without a parent, Windows can open the box behind the window (after a drag from
        Explorer, say); the window then looks frozen, because the hidden box blocks it.
        """
        root = self.root
        with contextlib.suppress(tk.TclError):
            root.deiconify()
            root.lift()
            root.focus_force()
        show = messagebox.showerror if error else messagebox.showinfo
        show(title, message, parent=root)

    # =============================================================== tools
    def open_vehicle_manager(self, select: str | None = None) -> None:
        """Open the vehicle library window (or bring it to the front)."""
        from .tools import VehicleManager

        if self.vehicle_manager:
            self.vehicle_manager.win.lift()
            self.vehicle_manager.render(select)
            return
        self.vehicle_manager = VehicleManager(self, select)

    def open_color_converter(self) -> None:
        """Open the sRGB / linear colour converter."""
        from .tools import ColorConverter

        ColorConverter(self)

    def open_tobj_editor(self) -> None:
        """Open the TOBJ editor."""
        from .tools import TobjEditor

        TobjEditor(self)

    def open_tutorials(self, topic: str | None = None) -> None:
        """Open the tutorials window, optionally at ``topic``."""
        from .tutorials import open_tutorials

        open_tutorials(self, topic)

    def about(self) -> None:
        """The About box."""
        messagebox.showinfo(
            "About Paintbooth",
            f"Paintbooth {__version__}\n\nFast, offline skin mod tool for "
            "Euro Truck Simulator 2 and American Truck Simulator.\nOpens and saves Mods Studio 2 "
            "projects (.pb, and Mods Studio 2 .ms2).\n\nIndependent clean-room software, not affiliated "
            "with SCS Software or Mods Studio.",
        )

    # =============================================================== updates
    def _check_updates(self) -> None:
        """Ask GitHub (in the background) whether a newer version exists; silent when offline."""
        if not self.settings.get("check_updates", True):
            return

        def work() -> None:
            from ..updates import check

            found = check(__version__)
            if found:
                self.ui.call(lambda: self._show_update(*found))

        threading.Thread(target=work, daemon=True).start()

    def open_github(self) -> None:
        """Open Paintbooth's GitHub page in the browser."""
        from ..updates import REPO_URL

        webbrowser.open(REPO_URL)

    def _show_update(self, version: str, url: str) -> None:
        """Tell the user about a new version: a message with "Don't tell me again", and the status bar.

        Clicking the status bar text opens the download page.
        """
        self._update_dialog(version, url)
        self.update_lbl.configure(text=f"Paintbooth {version} is available - click to download")
        ttk.Separator(self.statusbar, orient="vertical").pack(side="right", fill="y", padx=10)
        self.update_lbl.pack(side="right")
        self.update_lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
        Tooltip(
            self.update_lbl,
            f"Opens {url}\nInstall it by running the new Setup.exe; your projects and settings are kept.",
        )

    def _update_dialog(self, version: str, url: str) -> None:
        """A small window: 'Paintbooth X is available', Download / Later, and "Don't tell me again"."""
        win = tk.Toplevel(self.root)
        win.title("Update available")
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(
            f,
            text=f"Paintbooth {version} is available (you have {__version__}).",
            style="NoticeHead.TLabel",
        ).pack(anchor="w")
        ttk.Label(f, text="Install it by running the new Setup; your projects and settings are kept.").pack(
            anchor="w", pady=(6, 0)
        )
        never = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Don't tell me again", variable=never).pack(anchor="w", pady=(12, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(14, 0))

        def close(download: bool) -> None:
            if never.get():  # same switch as Settings > "Check for updates at start-up"
                self.settings["check_updates"] = False
                self.save_settings()
            win.destroy()
            if download:
                webbrowser.open(url)

        ttk.Button(bar, text="Later", command=lambda: close(False)).pack(side="right")
        ttk.Button(bar, text="Download", style="Accent.TButton", command=lambda: close(True)).pack(
            side="right", padx=(0, 8)
        )
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self.update_dialog = win

    # =============================================================== watch
    def _poll(self) -> None:
        """Every second: check the project file and the vehicle library for outside changes."""
        try:
            self.projects.check_outside_change()
            self.vehicles.check_changed()
        except OSError as e:  # e.g. a network drive went away; try again next time
            log.info("change check failed: %s", e)
        finally:
            self.root.after(POLL_MS, self._poll)

    # =============================================================== shutdown
    def _remember_layout(self) -> None:
        """Store the window geometry and the item-list width in the settings."""
        self.settings["geometry"] = self.root.geometry()
        try:
            w = self.pane.sashpos(0)
            if w >= MIN_LIST:
                self.settings["list_width"] = w
        except tk.TclError:
            pass  # the pane is gone; keep the stored width

    def restart(self) -> None:
        """Start a new Paintbooth process (with the same project) and close this one."""
        if not self._confirm_discard():
            return
        script = sys.argv[0] if sys.argv else ""
        if script and not script.endswith("__main__.py") and Path(script).is_file():
            args = [sys.executable, str(Path(script).resolve())]
        else:
            args = [sys.executable, "-m", "paintbooth", "app"]
        if self.project and self.project.path:
            args.append(str(self.project.path))
        self.settings["geometry"] = self.root.geometry()
        self.save_settings()
        try:
            subprocess.Popen(args, close_fds=True)
        except OSError as e:
            messagebox.showerror("Restart", f"Could not restart: {e}")
            return
        self.exports.close()
        self.root.destroy()

    def quit(self) -> None:
        """Close the window (after asking about unsaved changes) and save the settings."""
        if not self._confirm_discard():
            return
        self._remember_layout()
        self.save_settings()
        self.exports.close()
        self.root.destroy()


def _run_and_break(fn: Callable[[], object], _event: object = None) -> str:
    """Run a shortcut's command and stop Tk from also handling the key in the focused widget."""
    fn()
    return "break"


def _configure_tplbuild(settings: dict[str, Any]) -> None:
    """Give the template builder the live settings dict (it reads ``ms2_dir`` from it).

    ``tplbuild.configure`` keeps a reference, so later changes in Settings apply at once.
    """
    tplbuild.configure(settings)


class _LogStream:
    """A ``sys.stdout``/``sys.stderr`` replacement that writes lines to a logger.

    Used under ``pythonw`` (no console), so tracebacks printed by Tk go to the rotating log file.
    """

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, text: str) -> int:
        """Buffer ``text`` and log every complete line."""
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line)
        return len(text)

    def flush(self) -> None:
        """Log a pending partial line."""
        if self._buf.strip():
            self.logger.log(self.level, self._buf)
        self._buf = ""


def _setup_logging() -> None:
    r"""Log to ``<Paintbooth folder>\paintbooth.log``, rotated at 1 MB with 3 old copies kept.

    Without a console (``pythonw``), the launcher points stdout/stderr at that same file opened for
    appending; that handle is closed and replaced, so only the rotating handler writes the file.
    """
    try:
        log_file = appdirs.ensure() / "paintbooth.log"
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True
        )
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    out: TextIO | None = sys.stderr
    if out is None or Path(getattr(out, "name", "") or "").name == log_file.name:
        if out is not None:
            out.close()  # the launcher's plain append handle on the same file
        sys.stdout = sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR)
    else:  # started from a console: warnings also show there, as before logging was configured
        console = logging.StreamHandler(out)
        console.setLevel(logging.WARNING)
        root_logger.addHandler(console)


#: The logo shipped inside the package; the window icon when no .ico is installed next to the app.
LOGO = Path(__file__).resolve().parent.parent / "data" / "logo.png"


def _set_window_icon(root: tk.Tk) -> None:
    """Use the Paintbooth logo as the icon of every window (title bar, Alt+Tab, Linux docks).

    On Windows the installed ``paintbooth.ico`` replaces it afterwards (see :func:`_windows_setup`),
    because an .ico also carries the small sizes the taskbar prefers.
    """
    try:
        from PIL import Image, ImageTk

        with Image.open(LOGO) as im:
            sizes = [im.resize((s, s), Image.Resampling.LANCZOS) for s in (256, 64, 32, 16)]
        # typed as Any: Tk's stub for iconphoto only knows tkinter's own PhotoImage
        photos: list[Any] = [ImageTk.PhotoImage(i, master=root) for i in sizes]
        root.iconphoto(True, *photos)
        root._logo_photos = photos  # type: ignore[attr-defined]  # keep a reference: Tk does not
    except (OSError, ValueError, tk.TclError) as e:
        log.info("window icon not set: %s", e)


def _windows_app_id() -> None:
    """Windows only: give the process its own taskbar identity, before any window exists.

    The program runs as ``pythonw.exe``. Without its own AppUserModelID Windows groups the window
    with Python and shows Python's icon on the taskbar. The ID must be set before the first
    window is created, or the taskbar ignores it.
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AL3X.Paintbooth")  # type: ignore[attr-defined]
    except (OSError, AttributeError) as e:
        log.info("taskbar identity not set: %s", e)


def _windows_setup(root: tk.Tk) -> None:
    """Windows only: use the installed ``paintbooth.ico`` for the window and the taskbar button.

    The .ico carries the small sizes (16, 24, 32 px) the taskbar and title bar use, so it looks
    sharper there than the PNG logo set by :func:`_set_window_icon`.
    """
    ico = Path(__file__).resolve().parents[2] / "paintbooth.ico"
    if not ico.is_file():
        log.info("no %s; the window keeps the PNG logo", ico)
        return
    try:
        root.iconbitmap(str(ico))  # this window
        root.iconbitmap(default=str(ico))  # and every window opened later
    except tk.TclError as e:
        log.info("window icon not set from %s: %s", ico, e)


def main(argv: list[str] | None = None) -> None:
    """Start Paintbooth.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``); the first one, if given, is a
            project file to open.
    """
    args = sys.argv[1:] if argv is None else argv
    _setup_logging()
    if appdirs.migrate():  # Documents\Paint booth or Documents\Skinyard -> Documents\Paintbooth (once)
        log.info("moved the folder of an earlier version to %s", appdirs.home())
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]  # sharp text on HiDPI
        except (OSError, AttributeError) as e:
            log.info("DPI awareness not set: %s", e)
    if sys.platform == "win32":
        _windows_app_id()
    root = tk.Tk()
    _set_window_icon(root)
    if sys.platform == "win32":
        _windows_setup(root)
    App(root, Path(args[0]) if args else None)
    root.mainloop()
