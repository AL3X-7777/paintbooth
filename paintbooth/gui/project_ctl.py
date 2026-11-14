r"""Opening, saving and closing projects, and noticing when the file changes on disk.

:class:`ProjectController` is the part of the main window that deals with the project *file*:
New / Open / Save / Save as / Close, the recent list, the default save location
(``Documents\Paintbooth\Projects``, named after the mod), the "changed outside Paintbooth"
check, and the version-only save that automatic versioning does after an export. The
:class:`~.main.App` keeps the open project in ``app.project`` and delegates to this class.
"""

from __future__ import annotations

import logging
import re
import xml.parsers.expat
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

from .. import appdirs
from ..ms2 import PROJECT_EXT, PROJECT_TYPES, Ms2Project
from ..xmldom import Elem
from . import startscreen

if TYPE_CHECKING:
    from .main import App

log = logging.getLogger(__name__)

GAME_NAMES = {"ats": "American Truck Simulator", "ets2": "Euro Truck Simulator 2"}

# Characters Windows does not allow in file names (and control characters).
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# What loading a project can raise: unreadable file, not XML, not an MS2 project, or XML nested
# deeper than Python's recursion limit.
LOAD_ERRORS = (OSError, ValueError, xml.parsers.expat.ExpatError, RecursionError)


class ProjectController:
    """File-level project actions for the main window.

    Attributes:
        mtime: ``st_mtime_ns`` of the project file when Paintbooth last read or wrote it, or None
            for an unsaved project. A different value on disk means another program changed it.
    """

    def __init__(self, app: App) -> None:
        self.app = app
        self.mtime: int | None = None

    # ------------------------------------------------------------------ helpers
    def key(self) -> str:
        """The key the project's export history is stored under in the settings (its path)."""
        p = self.app.project
        return str(p.path) if p and p.path else "untitled"

    def confirm_discard(self) -> bool:
        """Ask to save unsaved changes. Returns False if the user cancelled the action."""
        p = self.app.project
        if not p or not p.dirty:
            return True
        r = messagebox.askyesnocancel("Unsaved changes", "Save changes to the current project?")
        if r is None:
            return False
        if r:
            return self.save()
        return True

    def _stamp(self, path: Path) -> None:
        """Record the file's modification time after reading or writing it."""
        self.mtime = path.stat().st_mtime_ns

    def remember_recent(self, p: Path) -> None:
        """Put ``p`` at the top of the recent list and save the settings."""
        startscreen.remember(self.app, p)
        self.app.save_settings()

    def default_path(self) -> Path | None:
        """A free file name in the projects folder, named after the mod (None if the folder can't be made)."""
        p = self.app.project
        try:
            folder = appdirs.projects_dir()
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        name = ((p.manifest.vars.value("display_name") if p else "") or "").strip() or "Untitled"
        stem = _BAD_FILENAME_CHARS.sub("_", name).strip(" .") or "Untitled"
        path = folder / f"{stem}{PROJECT_EXT}"
        n = 2
        while path.exists():
            path = folder / f"{stem} ({n}){PROJECT_EXT}"
            n += 1
        return path

    # ------------------------------------------------------------------ new / open / close
    def new_dialog(self) -> None:
        """New project for the same game as the open one (ATS when none is open)."""
        self.new_project(self.app.project.game if self.app.project else "ats")

    def new_project(self, game: str = "ats") -> None:
        """Start a new, unsaved project, pre-filled with the creator name and default description."""
        app = self.app
        if not self.confirm_discard():
            return
        app.project = Ms2Project.new(game)
        app.start.hide()
        if app.settings.get("creator_name"):
            app.project.manifest.vars.set_value("author", app.settings["creator_name"])
        if app.settings.get("default_description", "").strip():
            app.project.manifest.vars.set_value("description", app.settings["default_description"])
        self.mtime = None
        app.selected = "manifest"
        app.refresh_items()
        app.status(f"New {GAME_NAMES[game]} project - add a skin from the Home tab")

    def open_dialog(self) -> None:
        """Ask for a project file and open it."""
        p = filedialog.askopenfilename(
            title="Open project",
            initialdir=str(appdirs.projects_dir()),
            filetypes=[*PROJECT_TYPES, ("All files", "*.*")],
        )
        if p:
            self.open_project(Path(p))

    def open_project(self, path: Path) -> None:
        """Open a .pb or .ms2 project (after asking about unsaved changes)."""
        app = self.app
        if not self.confirm_discard():
            return
        try:
            app.project = Ms2Project.load(path)
        except LOAD_ERRORS as e:
            log.info("could not open %s: %s", path, e)
            messagebox.showerror("Open project", f"Could not open {path.name}:\n{e}")
            return
        self._stamp(path)
        self.remember_recent(path)
        app.start.hide()
        app.selected = "manifest" if not app.project.items else "item::0"
        app.refresh_items()
        app.status(f"Opened {path.name}: {len(app.project.items)} item(s)")

    def close_project(self) -> None:
        """Close the project and go back to the start screen."""
        app = self.app
        if not self.confirm_discard():
            return
        app.project = None
        self.mtime = None
        app.refresh_items()
        app.start.show()
        app.status("Ready")

    def seed_from_recent(self) -> Elem | None:
        """Copy item structure from a project the user opened before (keeps MS2 compatibility)."""
        for p in self.app.settings.get("recent", []):
            try:
                prj = Ms2Project.load(Path(p))
            except LOAD_ERRORS:
                continue  # moved, deleted or damaged: try the next one
            for t in prj.templates_el.children:
                if t.type() == "AdvancedTruckSkin":
                    return t
        return None

    # ------------------------------------------------------------------ save
    def save(self) -> bool:
        """Save the project; a new project goes straight into the projects folder, named after the mod.

        Returns:
            True if the project was written.
        """
        app = self.app
        project = app.project
        if not project:
            return False
        first_save = project.path is None
        target = project.path or self.default_path()
        if target is None:
            return self.save_as()
        try:
            saved = project.save(target)  # sets project.path only when the file was written
        except (OSError, ValueError) as e:  # ValueError: the new bytes would not reopen (see Ms2Project.save)
            messagebox.showerror("Save", f"Could not save: {e}")
            return False
        if first_save:
            self.remember_recent(saved)
            app.refresh_items()
        self._stamp(saved)
        app.update_title()
        app.status(f"Saved {saved.name}")
        return True

    def save_as(self) -> bool:
        """Save under a new name; the export history moves with the project.

        "Save as" always suggests a Paintbooth project (.pb); pick .ms2 in the type list to keep
        Mods Studio 2's format.
        """
        app = self.app
        project = app.project
        if not project:
            return False
        if project.path:
            suggested = project.path.stem + PROJECT_EXT
            init_dir = project.path.parent
        else:
            suggested = (self.default_path() or Path("My skins" + PROJECT_EXT)).name
            init_dir = appdirs.projects_dir()
        p = filedialog.asksaveasfilename(
            title="Save project as",
            defaultextension=PROJECT_EXT,
            initialdir=str(init_dir),
            filetypes=[("Paintbooth project", "*.pb"), ("Mods Studio 2 project", "*.ms2")],
            initialfile=suggested,
        )
        if not p:
            return False
        old_key = self.key()
        old_path = project.path
        project.path = Path(p)
        ok = self.save()
        if not ok:
            project.path = old_path  # a failed "Save as" must not redirect the next Ctrl+S
        if ok:
            new_key = self.key()
            for k in ("versions", "exports"):  # keep the export history with the renamed project
                d = app.settings.get(k, {})
                if old_key in d and new_key not in d:
                    d[new_key] = d[old_key]
            self.remember_recent(Path(p))
            app.refresh_items()
        return ok

    def save_version(self, project: Ms2Project, version: str) -> None:
        """Write a new ``package_version`` into the project file without saving other edits.

        Automatic versioning changes the version on every export. Saving the whole project for
        that would also commit every unrelated unsaved edit, and the user could no longer discard
        them. So the file on disk is re-read, only its version is changed, and it is written back.
        When the version is the *only* unsaved change, this is a normal save (so the title loses
        its ``*``). The other edits stay unsaved in the window.

        Args:
            project: The open project (must have a path).
            version: The version that was just exported.
        """
        path = project.path
        if path is None:
            return
        try:
            on_disk = Ms2Project.load(path)
        except LOAD_ERRORS as e:
            log.warning("could not update the version in %s: %s", path, e)
            return
        on_disk.manifest.vars.set_value("package_version", version)
        if Ms2Project(project.root.clone(), path).to_bytes() == on_disk.to_bytes():
            self.save()  # nothing but the version was unsaved
            return
        try:
            on_disk.save()
        except (OSError, ValueError) as e:  # ValueError: save() refuses bytes that would not reopen
            log.warning("could not update the version in %s: %s", path, e)
            return
        self._stamp(path)  # our own write is not an outside change

    # ------------------------------------------------------------------ outside changes
    def reload_from_disk(self) -> None:
        """Reload the project after another program changed the file, keeping the selection."""
        app = self.app
        app.banner(None)
        project = app.project
        if not project or not project.path:
            return
        try:
            fresh = Ms2Project.load(project.path)
        except LOAD_ERRORS as e:  # e.g. caught half-written by the other program
            log.info("could not reload %s: %s", project.path, e)
            app.status(f"Could not reload {project.path.name}: {e}")
            return
        sel = app.selected
        app.project = fresh
        self._stamp(project.path)
        app.selected = sel
        app.refresh_items()
        app.status("Reloaded - the project file was changed outside Paintbooth")

    def check_outside_change(self) -> None:
        """Called every second: reload, or warn if there are unsaved edits, when the file changed."""
        project = self.app.project
        if not (project and project.path and project.path.exists()):
            return
        m = project.path.stat().st_mtime_ns
        if self.mtime is not None and m != self.mtime:
            self.mtime = m
            if project.dirty:
                self.app.banner(
                    "The project file was changed outside Paintbooth. You also have unsaved changes."
                )
            else:
                self.reload_from_disk()
