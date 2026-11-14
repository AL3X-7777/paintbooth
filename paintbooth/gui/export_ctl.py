"""Exporting the open project to a .scs mod (F5, Shift+F5, File > Export...).

:class:`ExportController` picks the output file, works out the export-time manifest values
(automatic version, "by <creator>" and version suffixes, default description), then plans and
builds the mod on a worker thread so the window stays responsive.

Thread safety: the worker never touches the live project. The project tree is copied on the Tk
thread (:func:`snapshot`) before the thread starts, so edits made while the export runs can't
change the mod halfway through or invalidate item indices. The result goes back to the Tk thread
through ``app.ui``.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

from .. import appdirs
from ..builder import Builder, BuildResult
from ..model import Issue
from ..ms2 import Ms2Project
from ..ms2build import export_name, next_version, plan_ms2
from .project_ctl import GAME_NAMES
from .widgets import open_path

if TYPE_CHECKING:
    from .main import App

log = logging.getLogger(__name__)


def snapshot(project: Ms2Project) -> Ms2Project:
    """Return an independent copy of ``project`` (same path) for a worker thread to read."""
    return Ms2Project(project.root.clone(), project.path)


def _show_in_folder(out: Path) -> None:
    """Select the exported file in the file manager, or open its folder if it has gone."""
    open_path(out if out.exists() else out.parent)


@dataclass
class ExportJob:
    """Everything one export needs, fixed on the Tk thread before the worker starts."""

    project: Ms2Project  # the live project (only used on the Tk thread, after the build)
    out: Path
    key: str  # settings key of the project's export history
    name: str  # the project's own mod name
    version: str | None  # the automatic version, when automatic versioning is on
    overrides: dict[str, str] = field(default_factory=dict)  # export-time manifest values


class ExportController:
    """Builds the open project into a mod; owns the texture :class:`~paintbooth.builder.Builder`.

    Attributes:
        builder: The texture encoder and packer (its process pool lives as long as the window).
        exporting: True while an export runs (a second F5 is ignored).
        last_export: The file the last export wrote, for "Show folder".
    """

    def __init__(self, app: App) -> None:
        self.app = app
        # "spawn" so the worker processes don't inherit Tk's state (fork + Tk crashes on Linux).
        self.builder = Builder(appdirs.cache_dir(), mp_context=multiprocessing.get_context("spawn"))
        self.exporting = False
        self.last_export: Path | None = None

    def close(self) -> None:
        """Stop the builder's worker processes."""
        self.builder.close()

    # ------------------------------------------------------------------ folders and versions
    def mod_folder(self, game: str) -> Path:
        r"""The game's mod folder (from Settings, else ``Documents\<game>\mod``)."""
        custom = self.app.settings.get("mod_dirs", {}).get(game)
        return Path(custom) if custom else appdirs.documents() / GAME_NAMES[game] / "mod"

    def export_folder(self, game: str) -> Path:
        r"""Where F5 saves mods for ``game`` (from Settings, else ``Paintbooth\Exports\<GAME>``)."""
        d = self.app.settings.get("export_dirs", {}).get(game)
        return Path(d) if d else appdirs.exports_dir(game)

    def next_export_version(self) -> str:
        """The version the next export gets when automatic versioning is on."""
        project = self.app.project
        if not project:
            return "1.00"
        last = self.app.settings.get("versions", {}).get(self.app.projects.key())
        return next_version(project.manifest.vars.value("package_version"), last)

    # ------------------------------------------------------------------ export
    def export(self, ask: bool = False, to_mod_folder: bool = False) -> None:
        """Validate, then build the mod in the background.

        Args:
            ask: Ask for the output file (File > Export...).
            to_mod_folder: Write into the game's mod folder (Shift+F5).
        """
        app = self.app
        project = app.project
        if not project or self.exporting:
            return
        app.revalidate()
        if any(p.level == "error" for p in app.problems):
            app.show_problems_dialog()
            return
        key = app.projects.key()
        out = self._target(project, key, ask, to_mod_folder)
        if out is None:
            return
        job = self._job(project, out, key)
        self.exporting = True
        app.status(f"Exporting {out.name}…")
        copy = snapshot(project)  # F8: the worker reads this copy, never the tree being edited
        icons = (app.default_skin_icon(), app.default_mod_icon())
        threading.Thread(target=self._work, args=(job, copy, icons), daemon=True).start()

    def _target(self, project: Ms2Project, key: str, ask: bool, to_mod_folder: bool) -> Path | None:
        """Choose the output file. Returns None if the user cancelled a dialog."""
        settings = self.app.settings
        game = project.game
        stem = (project.path.stem if project.path else "my_mod").replace(" ", "_")
        if to_mod_folder:
            folder = self.mod_folder(game)
            if not folder.is_dir():
                d = filedialog.askdirectory(title=f"Your {GAME_NAMES[game]} mod folder")
                if not d:
                    return None
                folder = Path(d)
                settings.setdefault("mod_dirs", {})[game] = d
            return folder / f"{stem}.scs"
        prev = settings.get("exports", {}).get(key)
        if ask:
            init_dir = Path(prev).parent if prev else self.export_folder(game)
            chosen = filedialog.asksaveasfilename(
                title="Export mod",
                defaultextension=".scs",
                initialdir=str(init_dir),
                initialfile=f"{stem}.scs",
                filetypes=[("SCS files", "*.scs"), ("ZIP files", "*.zip")],
            )
            if not chosen:
                return None
            settings.setdefault("exports", {})[key] = chosen
            return Path(chosen)
        return Path(prev) if prev else self.export_folder(game) / f"{stem}.scs"

    def _job(self, project: Ms2Project, out: Path, key: str) -> ExportJob:
        """Work out the export-time manifest values; the project keeps its own name.

        With automatic versioning the new version is also written into the open project (so the
        Mod Version shown matches the mod), which marks it unsaved until the export succeeds.
        """
        settings = self.app.settings
        m = project.manifest.vars
        job = ExportJob(project, out, key, m.value("display_name"), None)
        if settings.get("auto_version"):
            job.version = self.next_export_version()
            job.overrides["package_version"] = job.version
            if m.value("package_version") != job.version:
                m.set_value("package_version", job.version)
                self.app.update_title()
        shown_name = export_name(job.name, settings, job.version or m.value("package_version"))
        if shown_name != job.name:
            job.overrides["display_name"] = shown_name
        if not m.value("description").strip() and settings.get("default_description", "").strip():
            job.overrides["description"] = settings["default_description"]
        return job

    def _work(self, job: ExportJob, copy: Ms2Project, icons: tuple[Path, Path]) -> None:
        """Worker thread: plan and build the mod, then hand the result to the Tk thread."""
        try:
            files, _probs = plan_ms2(copy, job.overrides, icons)
            res = self.builder.build_files(files, job.out, "fast", job.out.parent, log=lambda *a: None)
        except Exception as e:  # never leave the app stuck in "Exporting…"; the user sees the message
            log.exception("export of %s failed", job.out)
            res = BuildResult(ok=False, issues=[Issue("error", "", 0, f"Export failed: {e}")])
        self.app.ui.call(lambda: self._done(job, res))

    def _done(self, job: ExportJob, res: BuildResult) -> None:
        """Tk thread: record the version, report success or show the errors."""
        app = self.app
        self.exporting = False
        self.last_export = job.out
        if not res.ok:
            msg = "\n".join(i.message for i in res.issues) or "Unknown error"
            app.status("Export failed")
            messagebox.showerror("Export", msg)
            return
        if job.version is not None:
            app.settings.setdefault("versions", {})[job.key] = job.version
            app.save_settings()
            project = job.project
            if app.project is project and project.path is not None and project.dirty:
                app.projects.save_version(project, job.version)  # F31: only the version is saved
            if app.selected == "manifest":
                app.manifest_editor.refresh_hints()
        out = job.out
        if app.settings.get("export_notice", True):
            folder = out.parent
            app.notice.show(
                f"{out.name} exported successfully to",
                "",
                (str(folder), partial(_show_in_folder, out)),
            )
        shown = job.overrides.get("display_name", job.name)
        ver = f"  ·  v{job.version}" if job.version else ""
        app.status(
            f'Exported "{shown}"{ver} to {out}  ·  {res.files} files  ·  '
            f"{res.size_bytes / 1e6:.1f} MB  ·  {res.textures_encoded} converted, "
            f"{res.textures_cached} cached  ·  {res.seconds:.2f}s"
        )

    def show_export(self) -> None:
        """Show the last exported mod (or the export folder) in the file manager."""
        app = self.app
        last: Path | str | None = self.last_export
        if last is None and app.project:
            last = app.settings.get("exports", {}).get(app.projects.key())
            if last is None:
                last = self.export_folder(app.project.game)
        if last and Path(last).exists():
            open_path(Path(last))
        else:
            app.status("Nothing exported yet")
