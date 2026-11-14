"""The Settings window (ribbon > Program > Settings, or File > Settings).

It edits ``app.settings`` in place: nothing changes until Save, which writes every field back and
calls ``app.settings_changed()`` so the main window can re-theme, regenerate the default icons and
reload the editors. Each section of the window is built by its own ``_build_*`` method.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

from PIL import ImageTk

from .. import appdirs, ms2data, templates
from ..ms2build import export_name
from .psdmaker import import_dialog
from .widgets import ScrollFrame, Section, Tooltip, open_path
from .winutil import tool_window

if TYPE_CHECKING:
    from .main import App

LABEL_WIDTH = 24  # characters; keeps the field column aligned across all sections


class SettingsWindow:
    """The Settings tool window.

    Path fields are kept in :attr:`vars` under the settings key they save to (``export.ats``,
    ``mod.ets2``, ``image_dir``, ``ms2_dir``, ``icon_font``...).
    """

    def __init__(self, app: App) -> None:
        self.app = app
        self.win = tool_window(app, "settings", "Settings - Paintbooth", "820x700")
        sf = ScrollFrame(self.win)
        sf.pack(fill="both", expand=True)
        self._inner = sf.inner
        self.vars: dict[str, tk.StringVar] = {}
        self._photo: ImageTk.PhotoImage | None = None
        self._build_export()
        self._build_images()
        self._build_templates()
        self._build_ms2()
        self._build_creator()
        self._build_icon()
        self._build_appearance()
        bar = ttk.Frame(self.win, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Cancel", command=self.win.destroy).pack(side="right")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right", padx=6)
        self._preview()
        self._preview_icon()

    # ------------------------------------------------------------------ layout helpers
    def _section(self, title: str) -> ttk.Frame:
        """Add a collapsible section and return its body (a 3-column grid: label, field, button)."""
        x = Section(self._inner, title)
        x.pack(fill="x", padx=10, pady=(8, 0))
        x.body.columnconfigure(1, weight=1)
        return x.body

    @staticmethod
    def _label(body: ttk.Frame, row: int, text: str) -> None:
        """A right-aligned field label in column 0."""
        ttk.Label(body, text=text, anchor="e", width=LABEL_WIDTH).grid(
            row=row, column=0, sticky="e", padx=(4, 6), pady=3
        )

    def _path_row(
        self,
        body: ttk.Frame,
        row: int,
        label: str,
        key: str,
        default: Callable[[], str],
        kind: str = "dir",
        tip: str | None = None,
    ) -> tk.StringVar:
        """A label, an entry and a Browse button for a folder (``kind="dir"``) or a font file.

        Args:
            body: Section body.
            row: Grid row.
            label: Field label (a colon is added).
            key: Name of the variable in :attr:`vars`.
            default: Returns the text to start with.
            kind: ``"dir"`` for a folder, anything else for a font file.
            tip: Optional tooltip for the entry.
        """
        self._label(body, row, label + ":")
        var = tk.StringVar(value=default())
        self.vars[key] = var
        e = ttk.Entry(body, textvariable=var)
        e.grid(row=row, column=1, sticky="ew", pady=3)

        def browse() -> None:
            if kind == "dir":
                p = filedialog.askdirectory(parent=self.win, initialdir=var.get() or None)
            else:
                p = filedialog.askopenfilename(
                    parent=self.win, filetypes=[("Fonts", "*.ttf *.otf"), ("All files", "*.*")]
                )
            if p:
                var.set(str(Path(p)))

        ttk.Button(body, text="Browse", command=browse).grid(row=row, column=2, padx=(6, 4))
        if tip:
            Tooltip(e, tip)
        return var

    @staticmethod
    def _link(parent: ttk.Frame, text: str, action: Callable[[], object], padx: tuple[int, int]) -> ttk.Label:
        """A clickable link label packed to the left."""
        lb = ttk.Label(parent, text=text, style="Link.TLabel", cursor="hand2")
        lb.pack(side="left", padx=padx)
        lb.bind("<Button-1>", lambda e: action())
        return lb

    # ------------------------------------------------------------------ sections
    def _build_export(self) -> None:
        """Export and game mod folders, and the 'exported' message switch."""
        s, app = self.app.settings, self.app
        b = self._section("Export")
        self._path_row(
            b,
            0,
            "ATS export folder",
            "export.ats",
            lambda: s.get("export_dirs", {}).get("ats") or str(appdirs.exports_dir("ats")),
            tip="Where F5 saves American Truck Simulator mods (file name = project name).",
        )
        self._path_row(
            b,
            1,
            "ETS2 export folder",
            "export.ets2",
            lambda: s.get("export_dirs", {}).get("ets2") or str(appdirs.exports_dir("ets2")),
            tip="Where F5 saves Euro Truck Simulator 2 mods.",
        )
        self._path_row(
            b,
            2,
            "ATS game mod folder",
            "mod.ats",
            lambda: s.get("mod_dirs", {}).get("ats") or str(app.mod_folder("ats")),
            tip="Used by 'Export to mod folder' (Shift+F5).",
        )
        self._path_row(
            b,
            3,
            "ETS2 game mod folder",
            "mod.ets2",
            lambda: s.get("mod_dirs", {}).get("ets2") or str(app.mod_folder("ets2")),
        )
        self.export_notice = tk.BooleanVar(value=s.get("export_notice", True))
        ttk.Checkbutton(
            b,
            text="Show a message when a mod has been exported (closes on any key or click)",
            variable=self.export_notice,
        ).grid(row=4, column=1, sticky="w", pady=(6, 3))

    def _build_images(self) -> None:
        """Start folder of the image Browse buttons."""
        b = self._section("Images")
        self._path_row(
            b,
            0,
            "Open image dialogs in",
            "image_dir",
            lambda: self.app.settings.get("default_image_dir", ""),
            tip="Folder the Browse buttons for skins, accessories and icons start in. "
            "Empty = the last folder you used.",
        )

    def _build_templates(self) -> None:
        """Template pack folder, import button and download links."""
        app = self.app
        b = self._section("Templates")
        self._label(b, 0, "Template folder:")
        ttk.Label(b, text=str(appdirs.home() / "Templates")).grid(row=0, column=1, sticky="w")
        ttk.Button(b, text="Open", command=lambda: open_path(appdirs.templates_dir("ets2").parent)).grid(
            row=0, column=2, padx=(6, 4)
        )
        self.tpl_status = ttk.Label(b, text="", style="Muted.TLabel")
        self.tpl_status.grid(row=1, column=1, sticky="w")
        tr = ttk.Frame(b)
        tr.grid(row=2, column=1, sticky="w", pady=(6, 3))
        ttk.Button(
            tr,
            text="Import template pack (.zip)…",
            command=lambda: import_dialog(app, self.win, self._tpl_refresh),
        ).pack(side="left")
        ttk.Label(
            b,
            text="e.g. ETS2_templates (game ver. 1.59).zip, or unzip a pack into the folder above.",
            style="Muted.TLabel",
        ).grid(row=3, column=1, sticky="w")
        lk = ttk.Frame(b)
        lk.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(lk, text="Download (needs internet):").pack(side="left")
        for game in ("ets2", "ats"):
            text, url = templates.TEMPLATE_PACKS[game]
            Tooltip(self._link(lk, text, partial(webbrowser.open, url), (8, 0)), url)
        self._link(lk, "How to add them", lambda: app.open_tutorials("Getting template packs"), (14, 0))
        self._tpl_refresh()

    def _build_ms2(self) -> None:
        """Where Mods Studio 2 is installed (its templates build the non-skin items)."""
        b = self._section("Mods Studio 2")
        found = ms2data.find({}, None)  # auto-detection only, ignoring the folder set here
        self._path_row(
            b,
            0,
            "Mods Studio 2 folder",
            "ms2_dir",
            lambda: self.app.settings.get("ms2_dir", ""),
            tip="Lightbox, windscreen decal, door window, billboard, cargo container, company logo and "
            "existing mod items are built from Mods Studio 2's own template files. Empty = find it "
            "automatically (e.g. C:\\Mods Studio 2).",
        )
        ttk.Label(
            b,
            text=(
                f"Found automatically: {found}"
                if found
                else "Not found automatically - choose the folder with ModsStudio2UI.exe and Data."
            ),
            style="Muted.TLabel",
        ).grid(row=1, column=1, sticky="w")

    def _build_creator(self) -> None:
        """Creator name, name suffixes, automatic versions and the default description."""
        s = self.app.settings
        b = self._section("Creator and version")
        self._label(b, 0, "Default creator name:")
        self.creator = tk.StringVar(value=s.get("creator_name", ""))
        ttk.Entry(b, textvariable=self.creator).grid(row=0, column=1, sticky="ew", pady=3)
        self.append = tk.BooleanVar(value=s.get("append_creator", False))
        ttk.Checkbutton(
            b,
            text='Add "by <creator>" to the end of the mod name when exporting',
            variable=self.append,
            command=self._preview,
        ).grid(row=1, column=1, sticky="w", pady=3)
        self.example = ttk.Label(b, text="", style="Muted.TLabel")
        self.example.grid(row=2, column=1, sticky="w")
        self.creator.trace_add("write", lambda *a: self._preview())
        self.append.trace_add("write", lambda *a: self._preview())
        self.append_version = tk.BooleanVar(value=s.get("append_version", False))
        ttk.Checkbutton(
            b,
            text="Add the version number to the very end of the mod name when exporting",
            variable=self.append_version,
            command=self._preview,
        ).grid(row=3, column=1, sticky="w", pady=3)
        self.append_version.trace_add("write", lambda *a: self._preview())
        self.auto_version = tk.BooleanVar(value=s.get("auto_version", False))
        ttk.Checkbutton(
            b,
            text="Automatic version numbers (1.00, then 1.01, 1.02… on each export)",
            variable=self.auto_version,
            command=self._preview,
        ).grid(row=4, column=1, sticky="w", pady=(10, 3))
        ttk.Label(
            b,
            text="When on, the Mod Version field is hidden and the number goes up by itself.",
            style="Muted.TLabel",
        ).grid(row=5, column=1, sticky="w")
        ttk.Label(b, text="Default mod description:", anchor="ne", width=LABEL_WIDTH).grid(
            row=6, column=0, sticky="ne", padx=(4, 6), pady=(12, 3)
        )
        self.default_desc = tk.Text(
            b,
            height=5,
            wrap="word",
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            padx=4,
            pady=2,
            undo=True,
            font="TkTextFont",
        )
        self.default_desc.grid(row=6, column=1, columnspan=2, sticky="ew", pady=(12, 3), padx=(0, 4))
        self.default_desc.insert("1.0", s.get("default_description", ""))
        ttk.Label(
            b,
            text="New projects start with this text, and you can change it per project. "
            "A project with an empty description exports with this text.",
            style="Muted.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=7, column=1, columnspan=2, sticky="w")

    def _build_icon(self) -> None:
        """Font for the generated default icon, with a live preview."""
        b = self._section("Default icon")
        self._path_row(
            b,
            0,
            "Font file (optional)",
            "icon_font",
            lambda: self.app.settings.get("icon_font", ""),
            kind="file",
            tip="Font used for the 'paintbooth by AL3X' default icon. Empty = an installed font named "
            "Forgore, or a bold system font.",
        )
        self.font_status = ttk.Label(b, text="", style="Muted.TLabel")
        self.font_status.grid(row=1, column=1, sticky="w")
        self.icon_preview = ttk.Label(b)
        self.icon_preview.grid(row=2, column=1, sticky="w", pady=6)
        ttk.Button(b, text="Preview", command=self._preview_icon).grid(row=2, column=2, sticky="n", pady=6)
        self.vars["icon_font"].trace_add("write", lambda *a: self._preview_icon())

    def _build_appearance(self) -> None:
        """Theme, text size, UI scale, update check and the Paintbooth folder."""
        s = self.app.settings
        b = self._section("Appearance and folders")
        self.dark = tk.BooleanVar(value=s.get("theme", "dark") == "dark")
        ttk.Checkbutton(b, text="Dark mode (applies after restarting Paintbooth)", variable=self.dark).grid(
            row=0, column=1, sticky="w", pady=3
        )
        self._label(b, 3, "Text size:")
        fr = ttk.Frame(b)
        fr.grid(row=3, column=1, sticky="w", pady=3)
        self.font_size = tk.StringVar(value=str(s.get("font_size", 9)))
        ttk.Spinbox(
            fr, from_=7, to=20, increment=1, textvariable=self.font_size, width=5, justify="right"
        ).pack(side="left")
        ttk.Label(fr, text="pt  (default 9)", style="Muted.TLabel").pack(side="left", padx=(6, 0))
        self._label(b, 4, "UI scale:")
        fr = ttk.Frame(b)
        fr.grid(row=4, column=1, sticky="w", pady=3)
        self.ui_scale = tk.StringVar(value=f"{int(s.get('ui_scale', 100))} %")
        ttk.Combobox(
            fr,
            textvariable=self.ui_scale,
            width=8,
            values=[f"{v} %" for v in (80, 90, 100, 110, 125, 150, 175, 200)],
        ).pack(side="left")
        ttk.Label(
            fr,
            text="text and icons, on top of Windows' display scaling (default 100 %)",
            style="Muted.TLabel",
        ).pack(side="left", padx=(6, 0))
        self.check_updates = tk.BooleanVar(value=s.get("check_updates", True))
        ttk.Checkbutton(
            b,
            text="Check GitHub for a new version when Paintbooth starts (only when online; "
            "nothing is downloaded)",
            variable=self.check_updates,
        ).grid(row=5, column=1, sticky="w", pady=3)
        self._label(b, 2, "Paintbooth folder:")
        ttk.Label(b, text=str(appdirs.home())).grid(row=2, column=1, sticky="w")
        ttk.Button(b, text="Open", command=lambda: open_path(appdirs.ensure())).grid(
            row=2, column=2, padx=(6, 4)
        )

    # ------------------------------------------------------------------ live previews
    def _tpl_refresh(self) -> None:
        """Show how many template folders and PSDs each game's imported pack has."""
        parts = []
        for g, label in (("ets2", "ETS2"), ("ats", "ATS")):
            idx = templates.index(g)
            if idx:
                parts.append(f"{label}: {len(idx)} template folders, {sum(len(t.psds) for t in idx)} PSD")
        self.tpl_status.configure(text="  ·  ".join(parts) or "No template pack imported yet.")

    def _preview(self) -> None:
        """Show the exported mod name the current choices would give."""
        if not hasattr(self, "auto_version"):
            return  # a trace fired while the section is still being built
        project = self.app.project
        name = (project.manifest.vars.value("display_name") if project else "") or "My mod"
        if project and self.auto_version.get():
            version = self.app.next_export_version()
        else:
            version = (project.manifest.vars.value("package_version") if project else "") or "1.00"
        shown = export_name(
            name,
            {
                "append_creator": self.append.get(),
                "creator_name": self.creator.get().strip(),
                "append_version": self.append_version.get(),
            },
            version,
        )
        self.example.configure(text=f"Example: {shown}" if shown != name else "")

    def _preview_icon(self) -> None:
        """Render the default skin icon with the chosen font."""
        f = appdirs.find_font(self.vars["icon_font"].get().strip() or None)
        self.font_status.configure(
            text=f"Using: {f}" if f else "Forgore not found - using a bold system font"
        )
        img = appdirs.render_icon(appdirs.SKIN_ICON_SIZE, font_path=f)
        self._photo = ImageTk.PhotoImage(img)
        self.icon_preview.configure(image=self._photo)

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        """Copy every field into the settings, tell the main window, and close."""
        s = self.app.settings
        v = self.vars
        s["export_dirs"] = {"ats": v["export.ats"].get().strip(), "ets2": v["export.ets2"].get().strip()}
        s["mod_dirs"] = {"ats": v["mod.ats"].get().strip(), "ets2": v["mod.ets2"].get().strip()}
        s["default_image_dir"] = v["image_dir"].get().strip()
        s["ms2_dir"] = v["ms2_dir"].get().strip()
        s["creator_name"] = self.creator.get().strip()
        s["append_creator"] = self.append.get()
        s["auto_version"] = self.auto_version.get()
        s["export_notice"] = self.export_notice.get()
        s["check_updates"] = self.check_updates.get()
        s["append_version"] = self.append_version.get()
        s["default_description"] = self.default_desc.get("1.0", "end-1c").rstrip()
        font_changed = s.get("icon_font", "") != v["icon_font"].get().strip()
        s["icon_font"] = v["icon_font"].get().strip()
        s["theme"] = "dark" if self.dark.get() else "light"
        fs, sc = (
            self._read_size(self.font_size.get(), 7, 20, 9),
            self._read_size(self.ui_scale.get(), 70, 250, 100),
        )
        look_changed = (fs, sc) != (s.get("font_size", 9), s.get("ui_scale", 100))
        s["font_size"], s["ui_scale"] = fs, sc
        self.app.settings_changed(font_changed, look_changed)
        self.win.destroy()

    @staticmethod
    def _read_size(text: str, lo: int, hi: int, default: int) -> int:
        """Parse a number such as ``"12"`` or ``"125 %"``, clamped to ``lo``-``hi``.

        Returns ``default`` when the text is not a number.
        """
        try:
            return max(lo, min(hi, int(float(text.replace("%", "").strip()))))
        except ValueError:
            return default
