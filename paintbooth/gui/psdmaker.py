"""Template folder bar for the Skins tab, and template-pack import.

Templates are the SCS forum's PSD/PNG packs, imported into ``Documents/Paintbooth/Templates/<GAME>``
by :mod:`paintbooth.templates`. :class:`TemplateBar` shows which folder belongs to the skin's vehicle;
:func:`import_dialog` (Settings > Templates) unpacks a pack on a worker thread and reports back
through ``App.ui`` (the Tk-thread queue).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import appdirs, templates
from .widgets import Tooltip, open_path

log = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------------ helpers
def template_for(app: Any, item: Any) -> templates.TemplateDir | None:
    """The template folder for ``item``'s vehicle (honouring folders the user picked), or None."""
    if item is None or not app.project:
        return None
    v = item.vehicle()
    if v is None:
        return None
    return templates.find(v, app.project.game, app.settings.get("template_map", {}))


# ------------------------------------------------------------------------------------------ Skins tab bar
class TemplateBar(ttk.Frame):
    """The Skins tab's "Templates: <folder>" line, with Open / Choose folder / Get templates.

    Args:
        master: Parent frame (the top of the Skins tab).
        editor: The :class:`~paintbooth.gui.editors.TruckSkinEditor` whose item it describes.
    """

    def __init__(self, master: Any, editor: Any):
        super().__init__(master)
        self.editor = editor
        self.app = editor.app
        ttk.Label(self, text="Templates:", font="SkBold").pack(side="left")
        self.lbl = ttk.Label(self, text="", style="Muted.TLabel")
        self.lbl.pack(side="left", padx=(6, 0))
        self.choose_btn = ttk.Button(self, text="Choose folder…", command=self.choose)
        self.choose_btn.pack(side="right", padx=6)
        Tooltip(self.choose_btn, "Pick the template folder for this vehicle yourself (remembered)")
        self.open_btn = ttk.Button(self, text="Open folder", command=self.open)
        self.open_btn.pack(side="right")
        self.get_btn = ttk.Button(
            self, text="Get templates…", command=lambda: self.app.open_tutorials("Getting template packs")
        )
        Tooltip(self.get_btn, "Where to download the template packs and how to add them")
        self.td: templates.TemplateDir | None = None

    def refresh(self) -> None:
        """Look the folder up again (the vehicle, the template packs or the user's choice changed)."""
        it = self.editor.item
        v = it.vehicle() if it is not None else None
        self.td = template_for(self.app, it)
        game = self.app.project.game if self.app.project else "ets2"
        if v is None:
            self.lbl.configure(text="choose the vehicle first")
        elif self.td is not None:
            n_psd = len(self.td.psds)
            self.lbl.configure(text=f"{self.td.rel}   ({n_psd} PSD, {len(self.td.files) - n_psd} other)")
        elif not templates.index(game):
            self.lbl.configure(text="no template pack imported yet - Settings > Templates")
        else:
            self.lbl.configure(text=f"no folder found for {v.name or v.id} - click Choose folder…")
        self.open_btn.state(["!disabled"] if self.td is not None else ["disabled"])
        if not templates.index(game):
            self.get_btn.pack(side="right", padx=(0, 6))
        else:
            self.get_btn.pack_forget()
        self.choose_btn.state(["!disabled"] if v is not None else ["disabled"])

    def open(self) -> None:
        """Open the template folder in the file manager."""
        if self.td:
            open_path(self.td.path)

    def choose(self) -> None:
        """Let the user pick the folder for this vehicle; remembered as ``template_map["game:id"]``."""
        it = self.editor.item
        v = it.vehicle() if it is not None else None
        if v is None:
            return
        game = self.app.project.game
        start = self.td.path if self.td else appdirs.templates_dir(game)
        p = filedialog.askdirectory(
            title=f"Template folder for {v.name or v.id}", initialdir=str(start), mustexist=True
        )
        if p:
            self.app.settings.setdefault("template_map", {})[f"{game}:{v.id}"] = str(Path(p))
            self.refresh()


# ------------------------------------------------------------------------------------------ import
def import_dialog(app: Any, parent: Any = None, on_done: Callable[[], object] | None = None) -> None:
    """Ask for a template pack .zip (and its game, if the name doesn't say) and import it."""
    p = filedialog.askopenfilename(
        parent=parent,
        title="Template pack (.zip)",
        filetypes=[("Template pack", "*.zip"), ("All files", "*.*")],
    )
    if not p:
        return
    src = Path(p)
    game = templates.guess_game(src.name)
    if game is None:
        ans = messagebox.askyesnocancel(
            "Import templates",
            f"Is {src.name} for Euro Truck Simulator 2?\n\nYes = ETS2,  No = ATS",
            parent=parent,
        )
        if ans is None:
            return
        game = "ets2" if ans else "ats"
    run_import(app, src, game, on_done)


def run_import(app: Any, src: Path, game: str, on_done: Callable[[], object] | None = None) -> None:
    """Import ``src`` for ``game`` on a worker thread; progress and the result go to the status bar.

    Only one import runs at a time (``app._tpl_busy``). ``on_done`` runs on the Tk thread afterwards,
    whether the import worked or not.
    """
    if getattr(app, "_tpl_busy", False):
        return
    app._tpl_busy = True

    def prog(i: int, n: int, name: str) -> None:
        app.ui.call(lambda: app.status(f"Importing templates: {i}/{n}  {name}"))

    def work() -> None:
        err = None
        try:
            n = templates.import_pack(src, game, prog)
        except Exception as e:  # a worker thread must report every failure, never die silently
            log.exception("template pack import failed: %s", src)
            err, n = str(e), 0

        def done() -> None:
            app._tpl_busy = False
            if err:
                messagebox.showerror("Import templates", f"Could not import {src.name}:\n{err}")
            else:
                found = len(templates.index(game))
                app.status(
                    f"Imported {n} file(s) into Templates\\{'ETS2' if game == 'ets2' else 'ATS'} - "
                    f"{found} template folder(s)"
                )
            if getattr(app, "truck_editor", None) is not None and hasattr(app.truck_editor, "tpl_bar"):
                app.truck_editor.tpl_bar.refresh()
            if on_done:
                on_done()

        app.ui.call(done)

    threading.Thread(target=work, daemon=True).start()
