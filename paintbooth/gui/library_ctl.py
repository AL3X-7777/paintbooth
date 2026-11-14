r"""The user's vehicle library (``Documents\Paintbooth\Vehicles\*.xml``) as seen by the main window.

:class:`LibraryController` loads the library, reloads it when its files change (checked every
second by the main window), merges it with vehicle definitions stored inside the open project
into one catalogue for the truck picker, and runs the game scanner in the background.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

from .. import appdirs, vehlib
from .project_ctl import GAME_NAMES

if TYPE_CHECKING:
    from .main import App

log = logging.getLogger(__name__)


class LibraryController:
    """Vehicle library state and actions.

    Attributes:
        library_dir: The library folder.
        library: The loaded vehicles.
        library_errors: One message per XML file that could not be read.
    """

    def __init__(self, app: App) -> None:
        self.app = app
        self.library_dir = vehlib.library_dir()
        self.library: vehlib.Library = vehlib.Library()
        self.library_errors: list[str] = []
        self._sig: tuple[tuple[str, int], ...] = ()

    def _signature(self) -> tuple[tuple[str, int], ...]:
        """Names and modification times of the library's XML files (a change means reload)."""
        d = self.library_dir
        if not d.is_dir():
            return ()
        return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in d.rglob("*.xml")))

    def reload(self) -> list[str]:
        """Load the library again and refresh the views that show it.

        Returns:
            The load errors (also kept in :attr:`library_errors`).
        """
        app = self.app
        self.library, self.library_errors = vehlib.load_library(self.library_dir)
        self._sig = self._signature()
        # The first load happens before the editors exist.
        if getattr(app, "truck_editor", None) is not None and app.shown is app.truck_editor:
            app.truck_editor.render_vehicles()
        if app.vehicle_manager:
            app.vehicle_manager.refresh()
        return self.library_errors

    def check_changed(self) -> None:
        """Called every second: reload if a library file was added, removed or edited."""
        if self._signature() != self._sig:
            self.reload()
            if self.app.shown is self.app.truck_editor:
                self.app.truck_editor._update_truck_footer()
            self.app.status("Vehicle library reloaded")

    def vehicle_catalog(self) -> dict[str, tuple[vehlib.Vehicle, str]]:
        """Key -> (vehicle, origin), origin being ``"library"`` or ``"project"``.

        Definitions stored in the project are listed unless the library has the very same
        definition; one whose id clashes with a different library entry gets a suffixed key.
        """
        cat: dict[str, tuple[vehlib.Vehicle, str]] = {}
        project = self.app.project
        game = project.game if project else ""
        for key, v in self.library.items():
            cat[key] = (v, "library")
        if project:
            for vid, v in project.embedded_vehicles().items():
                if any(c.same_content(v) for c in self.library.with_id(vid, game)):
                    continue
                key = vid if vid not in cat else f"{vid}  [stored in this project]"
                v.key = key
                cat[key] = (v, "project")
        return cat

    def catalog_key(self, v: vehlib.Vehicle | None) -> str | None:
        """Key of the catalogue entry matching an item's embedded vehicle definition."""
        if v is None:
            return None
        for key, (c, _) in self.vehicle_catalog().items():
            if c.id == v.id and c.same_content(v):
                return key
        return None

    def auto_coaches(self) -> None:
        """Add the Coaches DLC's coaches to the library without a manual scan, once.

        Runs at start-up. If the library has no coaches yet and ETS2 with ``dlc_bus.scs`` is found
        (the folder from Settings, else the Steam libraries), the DLC archive alone is scanned on a
        worker thread: the coaches are added, their templates written and the game folder
        remembered for exports.
        """
        app = self.app
        if any(v.is_bus for v in self.library.values()):
            return
        known = (app.settings.get("game_dirs") or {}).get("ets2")
        game = Path(known) if known else appdirs.find_game_dir("ets2")
        if game is None or not (game / "dlc_bus.scs").is_file():
            return
        app.settings.setdefault("game_dirs", {})["ets2"] = str(game)
        app.status("Adding the coaches from the Coaches DLC…")
        threading.Thread(target=self._scan, args=(game / "dlc_bus.scs", "ets2"), daemon=True).start()

    @staticmethod
    def _bus_templates(src: Path, vehicles: list[vehlib.Vehicle]) -> int:
        """Worker thread: write the UV templates of any scanned coaches; return how many files.

        A failure only costs the templates (it is logged); the scan result still counts.
        """
        buses = [v for v in vehicles if v.is_bus]
        if not buses:
            return 0
        from ..coach import write_templates
        from ..scanner import game_reader

        try:
            return len(write_templates(game_reader(src), buses, appdirs.templates_dir("ets2") / "Buses"))
        except (OSError, ValueError):
            log.exception("could not write the coach templates")
            return 0

    def scan_dialog(self, folder: bool = True) -> None:
        """Ask for the game folder (or a mod archive) and add the trucks found to the library.

        Scanning runs on a worker thread. Existing library files are never overwritten, because
        the user may have edited them.

        Args:
            folder: True to pick the game folder, False to pick one .scs/.zip archive.
        """
        app = self.app
        game = app.project.game if app.project else "ats"
        if folder:
            guess = app.settings.get("game_dirs", {}).get(game) or ""
            chosen = filedialog.askdirectory(
                title=f"{GAME_NAMES[game]} game folder (contains def.scs)",
                initialdir=guess or None,
                mustexist=True,
            )
        else:
            chosen = filedialog.askopenfilename(
                title="Truck mod or game archive",
                filetypes=[("SCS archives", "*.scs *.zip"), ("All files", "*.*")],
            )
        if not chosen:
            return
        src = Path(chosen)
        if folder and (src / "def.scs").is_file():
            app.settings.setdefault("game_dirs", {})[game] = str(src)
        app.status(f"Scanning {src.name}… (this can take a minute for the full game)")
        threading.Thread(target=self._scan, args=(src, game), daemon=True).start()

    def _scan(self, src: Path, game: str) -> None:
        """Worker thread: scan ``src`` and save new vehicles, then report on the Tk thread."""
        from ..scanner import scan_vehicles  # slow import, only needed here

        app = self.app
        try:
            vehicles = scan_vehicles(src, game, log=lambda *a: None)
        except Exception as e:  # any scanner failure is shown to the user, never lost in the thread
            log.exception("scan of %s failed", src)
            msg = f"Scan failed: {e}"
            app.ui.call(lambda: messagebox.showerror("Scan", msg))
            return
        written = []
        for v in vehicles:
            if any(Path(x.source).exists() for x in self.library.with_id(v.id, v.game)):
                continue  # never overwrite files the user may have edited
            written.append(vehlib.save_to_library(v, self.library_dir, f"Scanned from {src.name}"))
        templates = self._bus_templates(src, vehicles)

        def done() -> None:
            self.reload()
            kept = len(vehicles) - len(written)
            app.status(
                f"Scan found {len(vehicles)} vehicle(s); added {len(written)} to your library"
                + (f", kept {kept} existing file(s) unchanged" if kept else "")
                + (f"; wrote {templates} coach templates to Templates\\ETS2\\Buses" if templates else "")
            )

        app.ui.call(done)
