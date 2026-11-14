"""Editors for the two built-in kinds of project item: the mod manifest and Advanced Skins.

``App`` shows one editor at a time on the right of the main window:

* :class:`ManifestEditor` for the project's :class:`~paintbooth.ms2.Manifest` (one scrolling page of
  collapsible sections);
* :class:`TruckSkinEditor` for an :class:`~paintbooth.ms2.AdvancedTruckSkin` (a notebook with the
  Vehicle, Main, Skins and Accessories tabs).

Template items (lightbox, billboard, ...) use :class:`~paintbooth.gui.tpleditor.TemplateEditor`.

Every widget writes straight into the item (and calls ``App.changed``) as the user types, so the
project is always current. Looking a typed path up on disk can walk the project folder
(:func:`paintbooth.ms2build.resolve`), so previews of typed paths go through a :class:`Debouncer`
and only run once typing pauses.

Uses these ``App`` attributes: ``project``, ``settings``, ``thumbs``, ``library``, ``library_dir``,
``changed()``, ``revalidate_soon()``, ``show_problems()``, ``status()``, ``project_dir()``,
``image_dir()``, ``remember_image_dir()``, ``default_mod_icon()``, ``default_skin_icon()``,
``installed_dlcs()``, ``next_export_version()``, ``vehicle_catalog()``, ``catalog_key()``,
``reload_library()``, ``scan_dialog()``, ``open_vehicle_manager()`` and ``open_tutorials()``.
"""

from __future__ import annotations

import contextlib
import functools
import math
import tkinter as tk
from collections.abc import Callable, Iterable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any

from .. import vehlib
from ..ms2 import AdvancedTruckSkin, Manifest
from ..ms2build import export_name, image_size, resolve
from ..ms2skeleton import CATEGORIES
from .icons import shared as icons
from .theme import C
from .widgets import CheckList, ColorRow, Help, Mark, Preview, ScrollFrame, Section, Tooltip

if TYPE_CHECKING:
    from .psdmaker import TemplateBar

PATH_DEBOUNCE_MS = 300  # wait this long after the last keystroke before looking a typed path up

Option = tuple[str, str, str]  # (value, text, group) as CheckList.set_items takes them
DEFAULT_ICON_TIP = (
    "The Paintbooth default icon (black, 'paintbooth by AL3X'). Used whenever no icon is chosen."
)


class Debouncer:
    """Call ``fn`` once, ``ms`` milliseconds after the last call of this object.

    Used for work that should follow typing but not run on every keystroke (file lookups,
    image previews). Calls after ``widget`` was destroyed are dropped.
    """

    def __init__(self, widget: tk.Misc, ms: int, fn: Callable[[], object]):
        self.widget, self.ms, self.fn = widget, ms, fn
        self._job: str | None = None

    def __call__(self, *_: object) -> None:
        """(Re)start the timer. Extra arguments (from Tk traces and bindings) are ignored."""
        self.cancel()
        try:
            self._job = self.widget.after(self.ms, self._run)
        except tk.TclError:  # the application is shutting down
            self._job = None

    @property
    def pending(self) -> bool:
        """True while a call is waiting to run."""
        return self._job is not None

    def cancel(self) -> None:
        """Drop the waiting call, if any."""
        if self._job is not None:
            with contextlib.suppress(tk.TclError):  # already ran, or the interpreter is gone
                self.widget.after_cancel(self._job)
            self._job = None

    def flush(self) -> None:
        """Run the waiting call now (tests, and before something needs the result)."""
        if self.pending:
            self.cancel()
            self._run()

    def _run(self) -> None:
        """Timer fired: call ``fn`` unless the widget no longer exists."""
        self._job = None
        try:
            alive = bool(self.widget.winfo_exists())
        except tk.TclError:
            return
        if alive:
            self.fn()


class Form:
    """Grid helper for label | help | widget | mark rows (column 2 stretches)."""

    def __init__(self, parent: tk.Misc, label_width: int = 20):
        self.parent = parent
        self.row = 0
        self.marks: dict[str, Mark] = {}  # problem marks by field name, for show_problems
        parent.columnconfigure(2, weight=1)
        self.label_width = label_width

    def add(
        self,
        label: str,
        widget: tk.Widget,
        field: str | None = None,
        help: str | None = None,
        sticky: str = "ew",
        pady: int = 3,
    ) -> tk.Widget:
        """Grid ``widget`` on the next row, with its label, an optional "?" help and a mark.

        Args:
            label: Text left of the widget ("" for none); a colon is added.
            widget: The input widget (already created with ``self.parent`` as master).
            field: Item field name; if given, a :class:`Mark` is added and kept in :attr:`marks`.
            help: Tooltip text for a "?" icon.
            sticky: Grid sticky of the widget; "nsew" also top-aligns the label, help and mark.
            pady: Vertical padding of the row.

        Returns:
            ``widget``.
        """
        top = sticky == "nsew"  # tall widgets: keep label/help/mark at the top of the row
        if label:
            ttk.Label(self.parent, text=label + ":", anchor="e", width=self.label_width).grid(
                row=self.row, column=0, sticky="ne" if top else "e", padx=(8, 4), pady=pady
            )
        if help:
            Help(self.parent, help).grid(row=self.row, column=1, sticky="n" if top else "", padx=(0, 6))
        widget.grid(row=self.row, column=2, sticky=sticky, pady=pady)
        if field:
            m = Mark(self.parent)
            m.grid(row=self.row, column=3, sticky="n" if top else "", padx=(4, 8))
            self.marks[field] = m
        self.row += 1
        return widget

    def section(self, title: str) -> None:
        """A bold heading with a rule across the form."""
        f = ttk.Frame(self.parent)
        ttk.Label(f, text=title, style="Section.TLabel").pack(side="left")
        ttk.Separator(f).pack(side="left", fill="x", expand=True, padx=(10, 0))
        f.grid(row=self.row, column=0, columnspan=4, sticky="ew", padx=8, pady=(16 if self.row else 6, 6))
        self.row += 1


def _bind_entry(var: tk.StringVar, setter: Callable[[str], object], editor: Any) -> None:
    """Call ``setter(value)`` whenever ``var`` changes, except while ``editor.loading``."""
    var.trace_add("write", lambda *a: (not editor.loading) and setter(var.get()))


def _set_marks(marks: dict[str, Mark], probs: Iterable[Any], ok_fields: Iterable[str] = ()) -> None:
    """Show each field's worst problem on its mark; fields in ``ok_fields`` show a tick when clean."""
    probs = list(probs)
    for k, m in marks.items():
        mine = [p for p in probs if p.field == k]
        if mine:
            m.set(_worst(mine), "\n".join(p.message for p in mine))
        else:
            m.set("ok" if k in ok_fields else None)


def _worst(probs: list[Any]) -> str:
    """Return "error" if any of ``probs`` is an error, else "warning"."""
    return "error" if any(p.level == "error" for p in probs) else "warning"


# ==========================================================================
# Mod manifest
# ==========================================================================


class ManifestEditor(ttk.Frame):
    """Mod manifest: one page of collapsible sections."""

    SECTIONS = ("Basic", "Icon", "Text", "Multiplayer", "Environment")

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.loading = False  # True while load() fills the widgets: their callbacks must not write
        self.item: Manifest | None = None
        self.marks: dict[str, Mark] = {}
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Mod manifest", font="SkSection").pack(side="left", padx=(4, 0))
        self.fold_btns = ttk.Frame(bar)
        ttk.Button(
            self.fold_btns, text="Collapse all", style="Tool.TButton", command=lambda: self._fold(False)
        ).pack(side="left")
        ttk.Button(
            self.fold_btns, text="Expand all", style="Tool.TButton", command=lambda: self._fold(True)
        ).pack(side="left", padx=(4, 0))
        self.container: ScrollFrame | None = None
        self.sections: list[Section] = []
        self._icon_soon = Debouncer(self, PATH_DEBOUNCE_MS, self._icon_preview)
        self._build()

    # ---------------------------------------------------------------- layout
    def _fold(self, open_: bool) -> None:
        """Open or close every section."""
        for sec in self.sections:
            sec.set_open(open_)

    def _build(self) -> None:
        """(Re)create all widgets. ``App`` calls this after Settings change (auto version, fonts)."""
        if self.container is not None:
            self.container.destroy()
        self.marks = {}
        self.sections = []
        self.desc_hint: ttk.Label | None = None
        store = self.app.settings.setdefault("sections", {})  # remembered open/closed state
        self.fold_btns.pack(side="right")
        sf = ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        self.container = sf
        parents = []
        for name in self.SECTIONS:
            sec = Section(sf.inner, name, f"manifest.{name}", store)
            sec.pack(fill="x", padx=4, pady=(4, 0))
            self.sections.append(sec)
            parents.append(sec.body)
        self._build_basic(parents[0])
        self._build_icon(parents[1])
        self._build_text(parents[2])
        self._build_mp(parents[3])
        self._build_env(parents[4])

    def _build_basic(self, basic: ttk.Frame) -> None:
        """Name, author, version, DLC dependencies, categories and compatible versions."""
        f = Form(basic, 18)
        self.name, self.author, self.version = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for var, key in (
            (self.name, "display_name"),
            (self.author, "author"),
            (self.version, "package_version"),
        ):
            _bind_entry(var, functools.partial(self._set, key), self)
        f.add(
            "Mod Name",
            ttk.Entry(basic, textvariable=self.name),
            "display_name",
            "Name shown in the in-game Mod Manager.",
        )
        self.name_hint = ttk.Label(basic, text="", style="Muted.TLabel")
        f.add("", self.name_hint, None, sticky="w", pady=0)
        f.add("Mod Author", ttk.Entry(basic, textvariable=self.author), "author", "Your name or team.")
        self._build_version(basic, f)
        self.dlcs = CheckList(basic, on_change=lambda sel: self._set_list("dlc_dependencies", sel), height=10)
        f.add(
            "DLC Dependencies",
            self.dlcs,
            "dlc_dependencies",
            "DLCs the mod needs. The game greys the mod out when one is missing.",
            sticky="nsew",
        )
        self.cats = CheckList(basic, on_change=lambda sel: self._set_list("category", sel), height=8)
        f.add("Mod Category", self.cats, "category", "Categories shown in the Mod Manager.", sticky="nsew")
        self.compat = tk.Text(
            basic, height=3, wrap="none", borderwidth=1, relief="solid", highlightthickness=0, padx=4, pady=2
        )
        self.compat.bind("<<Modified>>", self._compat_changed)
        f.add(
            "Compatible Versions",
            self.compat,
            "compatible_versions",
            "One game version per line, e.g. 1.56.*  (leave empty to skip the check).",
        )
        self.marks.update(f.marks)

    def _build_version(self, basic: ttk.Frame, f: Form) -> None:
        """Mod Version: a text field, or a read-only "next export" label with auto-versioning on."""
        self.version_info: ttk.Label | None
        if self.app.settings.get("auto_version"):
            self.version_info = ttk.Label(basic, text="", style="Muted.TLabel")
            f.add(
                "Mod Version",
                self.version_info,
                None,
                "Automatic: the first export is 1.00, then 1.01, 1.02… (turn off in Settings).",
                sticky="w",
            )
        else:
            self.version_info = None
            f.add(
                "Mod Version",
                ttk.Entry(basic, textvariable=self.version),
                "package_version",
                "Any text, e.g. 1.0 or v1.02.",
            )

    def _build_icon(self, icon: ttk.Frame) -> None:
        """Mod Manager icon: path, Browse, "Use skin image", "Use default icon" and a preview."""
        row = ttk.Frame(icon)
        row.pack(fill="x", padx=8)
        ttk.Label(row, text="Path:").pack(side="left")
        self.icon = tk.StringVar()
        _bind_entry(self.icon, self._icon_typed, self)
        ttk.Entry(row, textvariable=self.icon).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse", command=self._browse_icon).pack(side="left")
        b = ttk.Button(row, text="Use skin image", command=self._use_skin_image)
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, "Use the first skin image of the first skin as the icon.")
        b = ttk.Button(row, text="Use default icon", command=lambda: self.icon.set(""))
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, DEFAULT_ICON_TIP)
        m = Mark(row)
        m.pack(side="left", padx=4)
        self.marks["icon"] = m
        ttk.Label(
            icon,
            text="Shown in the Mod Manager. Any image works; it is resized to 276×162. "
            "Empty = the default icon.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=8, pady=(6, 4))
        self.icon_preview = Preview(icon, self.app.thumbs, 276 * 2, 162 * 2)
        self.icon_preview.pack(anchor="w", padx=8, pady=(0, 6))

    def _icon_typed(self, value: str) -> None:
        """Icon path edited: store it, and refresh the preview once typing pauses."""
        self._set("icon", value)
        self._icon_soon()

    def _build_text(self, text: ttk.Frame) -> None:
        """Description box, "Use default description" and the hint about the default."""
        ttk.Label(text, text="Mod Description:").pack(anchor="w", padx=8)
        self.desc = tk.Text(
            text,
            wrap="word",
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            padx=6,
            pady=4,
            undo=True,
            height=10,
        )
        self.desc.pack(fill="both", expand=True, padx=8, pady=4)
        self.desc.bind("<<Modified>>", self._desc_changed)
        row = ttk.Frame(text)
        row.pack(fill="x", padx=8)
        ttk.Label(
            row, text="Supports the game's colour tags, e.g. [orange]text[normal].", style="Muted.TLabel"
        ).pack(side="left")
        b = ttk.Button(row, text="Use default description", command=self._use_default_desc)
        b.pack(side="right")
        Tooltip(
            b, "Replace the text with your default description (set it in Settings > Creator and version)."
        )
        self.desc_hint = ttk.Label(text, text="", style="Muted.TLabel")
        self.desc_hint.pack(anchor="w", padx=8)

    def _build_mp(self, mp: ttk.Frame) -> None:
        """The "Multiplayer Optional" tick box."""
        inner = ttk.Frame(mp, padding=(12, 6))
        inner.pack(fill="x")
        self.mp = tk.BooleanVar()
        ttk.Checkbutton(
            inner,
            text="Multiplayer Optional",
            variable=self.mp,
            command=lambda: self._set_bool("multiplayer_optional", self.mp.get()),
        ).pack(anchor="w")
        ttk.Label(
            inner, text="Players can join Convoy sessions without having this mod.", style="Muted.TLabel"
        ).pack(anchor="w", pady=4)

    def _build_env(self, env: ttk.Frame) -> None:
        """The optional mod internal name."""
        f = Form(env, 18)
        self.gname = tk.StringVar()
        _bind_entry(self.gname, lambda v: self._set("GlobalInternalName", v), self)
        f.add(
            "Mod Internal Name",
            ttk.Entry(env, textvariable=self.gname),
            "GlobalInternalName",
            "Optional unit name of the mod package (a-z, 0-9, _).",
        )
        self.marks.update(f.marks)

    # ---------------------------------------------------------------- data
    def load(self, item: Manifest) -> None:
        """Fill every widget from ``item`` (no change is reported while doing so)."""
        self.loading = True
        self.item = item
        v = item.vars
        self.name.set(v.value("display_name"))
        self.author.set(v.value("author"))
        self.version.set(v.value("package_version"))
        dl = v.predefined("dlc_dependencies") or self.app.installed_dlcs()
        self.dlcs.set_items([(val, txt, grp) for val, txt, grp in dl], v.strings("dlc_dependencies"))
        cats = v.predefined("category") or [(c, c, "") for c in CATEGORIES]
        self.cats.set_items(cats, v.strings("category"))
        self.compat.delete("1.0", "end")
        self.compat.insert("1.0", "\n".join(v.strings("compatible_versions")))
        self.compat.edit_modified(False)
        self.icon.set(v.value("icon"))
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", v.value("description").replace("\r\n", "\n"))
        self.desc.edit_modified(False)
        self.mp.set(v.bool("multiplayer_optional"))
        self.gname.set(v.value("GlobalInternalName"))
        self.loading = False
        self._icon_soon.cancel()
        self._icon_preview()
        self.refresh_hints()

    def refresh_hints(self) -> None:
        """Update the "Exported as" name, the default-description hint and the next auto version."""
        if self.item is None:
            return
        s = self.app.settings
        name = self.name.get()
        version = self.app.next_export_version() if s.get("auto_version") else self.version.get()
        shown = export_name(name, s, version)
        self.name_hint.configure(text=f"Exported as:  {shown}" if shown != name else "")
        if self.desc_hint is not None:
            default = s.get("default_description", "").strip()
            self.desc_hint.configure(
                text="Empty: your default description (Settings) is used when exporting."
                if default and not self.desc.get("1.0", "end-1c").strip()
                else ""
            )
        if self.version_info is not None:
            self.version_info.configure(text=f"Automatic  ·  next export: {self.app.next_export_version()}")

    def _set(self, key: str, value: str) -> None:
        """Write a text variable (only if the manifest has it, as MS2 manifests may omit some)."""
        if self.loading or self.item is None:
            return
        if self.item.vars.get(key) is None:
            return
        self.item.vars.set_value(key, value)
        self.app.changed()
        if key in ("display_name", "package_version"):
            self.refresh_hints()

    def _set_bool(self, key: str, value: bool) -> None:
        """Write a boolean variable."""
        if not self.loading and self.item:
            self.item.vars.set_bool(key, value)
            self.app.changed()

    def _set_list(self, key: str, values: list[str]) -> None:
        """Write a string-list variable (DLCs, categories)."""
        if not self.loading and self.item:
            self.item.vars.set_strings(key, values)
            self.app.changed()

    def _compat_changed(self, _: object = None) -> None:
        """Compatible versions box edited: store its non-empty lines."""
        if not self.compat.edit_modified():
            return
        self.compat.edit_modified(False)  # re-arm <<Modified>>
        if self.loading or not self.item:
            return
        lines = [ln.strip() for ln in self.compat.get("1.0", "end").splitlines() if ln.strip()]
        self.item.vars.set_strings("compatible_versions", lines)
        self.app.changed()

    def _desc_changed(self, _: object = None) -> None:
        """Description edited: store it and refresh the default-description hint."""
        if not self.desc.edit_modified():
            return
        self.desc.edit_modified(False)  # re-arm <<Modified>>
        if self.loading or not self.item:
            return
        self.item.vars.set_value("description", self.desc.get("1.0", "end-1c"))
        self.app.changed()
        self.refresh_hints()

    def _use_default_desc(self) -> None:
        """Replace the description with the default from Settings (asking if it would lose text)."""
        default = self.app.settings.get("default_description", "")
        if not default.strip():
            messagebox.showinfo(
                "Mod description",
                "No default description yet.\n\nWrite one in Settings > Creator and version.",
            )
            return
        cur = self.desc.get("1.0", "end-1c")
        if (
            cur.strip()
            and cur != default
            and not messagebox.askyesno(
                "Mod description", "Replace the current description with your default description?"
            )
        ):
            return
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", default)

    def _browse_icon(self) -> None:
        """Pick the icon image."""
        p = filedialog.askopenfilename(
            title="Mod icon",
            initialdir=self.app.image_dir(),
            filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
        )
        if p:
            self.icon.set(str(Path(p)))

    def _use_skin_image(self) -> None:
        """Use the first skin image found in any Advanced Skin as the icon."""
        project = self.app.project
        for it in project.items if project else []:
            if isinstance(it, AdvancedTruckSkin):
                for e in it.entries("CabVariantSkin"):
                    p = resolve(e.image, self.app.project_dir())
                    if p is not None:
                        self.icon.set(str(p))
                        return
        messagebox.showinfo("Mod icon", "Add a skin image first (Skins tab of a skin).")

    def _icon_preview(self) -> None:
        """Show the chosen icon, or the default icon if none is set or found."""
        p = resolve(self.icon.get(), self.app.project_dir()) or self.app.default_mod_icon()
        self.icon_preview.show(p, "Default icon")

    def show_problems(self, probs: list[Any]) -> None:
        """Set the field marks from the manifest's ``Problem``s; the mod name shows a tick if clean."""
        _set_marks(self.marks, probs, ok_fields=("display_name",))


# ==========================================================================
# Advanced Truck Skin
# ==========================================================================


class ImageBlock(ttk.Frame):
    """One skin/accessory image with its 'suitable' checklist (MS2's image panel).

    Args:
        master: The page's holder frame.
        editor: The owning :class:`TruckSkinEditor`.
        array: "CabVariantSkin" (skin images) or "AccessorySkins" (accessory images).
        index: Position of the entry in that array.
    """

    def __init__(self, master: tk.Misc, editor: TruckSkinEditor, array: str, index: int):
        super().__init__(master, padding=8, style="Panel.TFrame")
        self.editor, self.array, self.index = editor, array, index
        self._loading = False
        self._preview_soon = Debouncer(self, PATH_DEBOUNCE_MS, self._update_preview)
        self._build_path_row(array)
        self.info_lbl = ttk.Label(self, text="", style="Muted.TLabel")
        self.info_lbl.configure(background=C["panel"])
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.preview = Preview(self, editor.app.thumbs, 480, 240)
        self.preview.grid(row=2, column=0, sticky="nw", pady=4)
        right = ttk.Frame(self, style="Panel.TFrame")
        right.grid(row=2, column=1, sticky="nsew", padx=(10, 0), pady=(4, 4))
        ttk.Label(
            right,
            text="Suitable variant:" if array == "CabVariantSkin" else "For accessories:",
            style="Panel.TLabel",
        ).pack(anchor="w")
        self.check = CheckList(right, on_change=self._suitable_changed, height=10, show_values=True)
        self.check.pack(fill="both", expand=True)
        ttk.Button(
            self,
            text=" Delete image",
            image=icons().get("delete", 16),
            compound="left",
            command=self._delete,
            style="Wide.TButton",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.columnconfigure(1, weight=1)

    def _build_path_row(self, array: str) -> None:
        """Top row: path field, Browse, (accessories) Color, and the problem mark."""
        top = ttk.Frame(self, style="Panel.TFrame")
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(top, text="Path:", style="Panel.TLabel").pack(side="left")
        self.path = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.path)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        self.path_entry = ent
        self.path.trace_add("write", lambda *a: self._path_changed())
        ttk.Button(top, text="Browse", command=self._browse).pack(side="left")
        if array == "AccessorySkins":
            b = ttk.Button(
                top,
                text=" Color",
                image=icons().get("palette", 16),
                compound="left",
                command=self._generate_color,
            )
            b.pack(side="left", padx=(6, 0))
            Tooltip(b, "Make a plain 64×64 colour texture (with opacity) for these accessories")
        self.mark = Mark(top)
        self.mark.configure(style="Panel.TLabel")
        self.mark.pack(side="left", padx=(4, 0))

    def load(self, entry: Any, options: list[Option]) -> None:
        """Show ``entry`` (a :class:`~paintbooth.ms2.SuitableImage`) with ``options`` to tick."""
        self._loading = True
        self.path.set(entry.image)
        self.check.set_items(options, entry.suitable)
        self._loading = False
        self._preview_soon.cancel()
        self._update_preview()

    def _entry(self) -> Any:
        """The project entry this block edits."""
        item = self.editor.item
        assert item is not None  # blocks exist only while an item is loaded
        return item.entries(self.array)[self.index]

    def focus_path(self) -> None:
        """New empty block: put the cursor in the path box (type/paste a path, or click Browse)."""
        self.path_entry.focus_set()

    def _path_changed(self) -> None:
        """Path typed: store it at once; look the file up once typing pauses."""
        if self._loading:
            return
        self._entry().image = self.path.get()
        self.editor.app.changed()
        self._preview_soon()

    def _update_preview(self) -> None:
        """Resolve the path and show the image, its size and where it was found."""
        p = resolve(self.path.get(), self.editor.app.project_dir())
        txt = ""
        if self.path.get() and p is None:
            txt = "File not found"
        elif p is not None:
            sz = image_size(p)
            txt = f"{p.name}   {sz[0]}×{sz[1]}" if sz else p.name
            if p != Path(self.path.get()):
                txt += f"   (found at {p})"
        self.info_lbl.configure(text=txt)
        self.preview.show(p, "Click Browse to choose an image (PNG or DDS)")

    def _browse(self) -> None:
        """Pick the image, starting in the current image's folder."""
        start = None
        cur = resolve(self.path.get(), self.editor.app.project_dir())
        if cur:
            start = str(cur.parent)
        p = filedialog.askopenfilename(
            title="Skin image",
            initialdir=start or self.editor.app.image_dir(),
            filetypes=[("Images", "*.png *.dds *.tga *.jpg *.psd"), ("All files", "*.*")],
        )
        if p:
            self.editor.app.remember_image_dir(Path(p).parent)
            self.path.set(str(Path(p)))

    def _generate_color(self) -> None:
        """Make a colour swatch DDS and use it as this image."""
        from .colorgen import ColorGenerator

        p = ColorGenerator(self.editor.app).run()
        if p:
            self.path.set(str(p))

    def _suitable_changed(self, sel: list[str]) -> None:
        """Ticks changed: store the cabins/accessories this image is for."""
        if self._loading:
            return
        self._entry().suitable = sel
        self.editor.app.changed()

    def _delete(self) -> None:
        """Remove this image from the item."""
        item = self.editor.item
        assert item is not None
        item.remove_entry(self.array, self.index)
        self.editor.app.changed()
        self.editor.rebuild_blocks(self.array)


class ImagesPage(ScrollFrame):
    """The Skins or Accessories tab: template bar (skins), Add buttons, hint, overview and images.

    Attributes:
        add_btn: The main "Add image" button.
        info_lbl: Hint text under the buttons.
        overview: (Accessories) frame with :attr:`pick`, shown until the first image exists.
        pick: (Accessories) the vehicle's paintable accessories, to tick before adding an image.
        holder: Frame the :class:`ImageBlock` widgets are packed into.
    """

    def __init__(self, editor: TruckSkinEditor, array: str):
        super().__init__(editor)
        self.top = ttk.Frame(self.inner)
        self.top.pack(fill="x", padx=8, pady=(8, 0))
        if array == "CabVariantSkin":
            from .psdmaker import TemplateBar

            editor.tpl_bar = TemplateBar(self.top, editor)
            editor.tpl_bar.pack(fill="x")
        # action buttons at the top, so they're visible however long the list below gets
        bar = ttk.Frame(self.inner)
        bar.pack(fill="x", padx=8, pady=(8, 0))
        add_txt = " Add image" if array == "CabVariantSkin" else " Add image for the ticked accessories"
        self.add_btn = ttk.Button(
            bar,
            text=add_txt,
            image=icons().get("image", 16),
            compound="left",
            style="Wide.TButton",
            command=lambda: editor.add_image(array),
        )
        self.add_btn.pack(side="left", fill="x", expand=True)
        if array == "AccessorySkins":
            b = ttk.Button(
                bar,
                text=" Generate color",
                image=icons().get("palette", 16),
                compound="left",
                style="Wide.TButton",
                command=editor.add_color,
            )
            b.pack(side="left", fill="x", expand=True, padx=(6, 0))
            Tooltip(
                b,
                "Add an accessory image that is a plain colour (64×64 DDS, any opacity) "
                "for the ticked accessories",
            )
        m = Mark(bar)
        m.pack(side="left", padx=4)
        editor.marks[array] = m
        self.info_lbl = ttk.Label(self.inner, text="", style="Muted.TLabel", wraplength=900, justify="left")
        self.info_lbl.pack(fill="x", padx=10, pady=(6, 0))
        if array == "AccessorySkins":
            # the vehicle's paintable accessories, shown straight away; tick some, then Add image
            self.overview = ttk.Frame(self.inner)
            self.pick = CheckList(self.overview, height=16, show_values=True)
            self.pick.pack(fill="both", expand=True)
        self.holder = ttk.Frame(self.inner)
        self.holder.pack(fill="x", padx=8, pady=8)


class TruckSkinEditor(ttk.Notebook):
    """Advanced Truck Skin editor: Vehicle, Main, Skins and Accessories tabs."""

    IMAGE_TABS = (("CabVariantSkin", "Skins"), ("AccessorySkins", "Accessories"))
    METALLIC = (
        "FlipColor",
        "FlakeColor",
        "FlipStrenght",  # sic: MS2's variable name
        "FlakeUVScale",
        "FlakeDensity",
        "FlakeShininess",
        "FlakeClearcoat",
        "FlipColorLocked",
        "FlakeColorLocked",
    )
    MASK = ("RedColor", "GreenColor", "BlueColor", "RedColorLocked", "GreenColorLocked", "BlueColorLocked")
    INT_FIELDS = ("SkinPrice", "SkinUnlock")
    FLOAT_FIELDS = ("FlipStrenght", "FlakeUVScale", "FlakeDensity", "FlakeShininess", "FlakeClearcoat")
    BOOL_FIELDS = (
        "AlternateUvSet",
        "BaseColorLocked",
        "EnableMetallic",
        "FlipColorLocked",
        "FlakeColorLocked",
        "EnableMask",
        "RedColorLocked",
        "GreenColorLocked",
        "BlueColorLocked",
    )

    _mute_select = False  # True while render_vehicles() restores the selection
    tpl_bar: TemplateBar  # the Skins tab's template folder bar (set by ImagesPage)

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.item: AdvancedTruckSkin | None = None
        self.loading = False  # True while load() fills the widgets: their callbacks must not write
        self.marks: dict[str, Mark] = {}
        self.blocks: dict[str, list[ImageBlock]] = {"CabVariantSkin": [], "AccessorySkins": []}
        self._icon_soon = Debouncer(self, PATH_DEBOUNCE_MS, self._icon_preview)
        self._build_truck_tab()
        self._build_skin_tab()
        self.pages: dict[str, ImagesPage] = {}
        for arr, title in self.IMAGE_TABS:
            page = ImagesPage(self, arr)
            self.add(page, text=title)
            self.pages[arr] = page

    def _need_item(self) -> AdvancedTruckSkin:
        """The loaded item (for code that only runs while one is loaded)."""
        assert self.item is not None
        return self.item

    # ---------------------------------------------------------------- truck
    def _build_truck_tab(self) -> None:
        """Vehicle tab: filter, vehicle tree, library-changed banner and footer buttons."""
        tab = ttk.Frame(self, padding=8)
        self.add(tab, text="Vehicle")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Truck or trailer:").pack(side="left")
        self.truck_mark = Mark(top)
        self.truck_mark.pack(side="left")
        self.marks["Truck"] = self.truck_mark
        self.vfilter = tk.StringVar()
        self.vfilter.trace_add("write", lambda *a: self.render_vehicles())
        ent = ttk.Entry(top, textvariable=self.vfilter, width=28)
        ent.pack(side="right")
        ttk.Label(top, image=icons().get("search", 14)).pack(side="right", padx=4)
        self.banner = ttk.Frame(tab, style="Banner.TFrame", padding=6)
        self.banner_lbl = ttk.Label(
            self.banner, text="", style="Banner.TLabel", wraplength=700, justify="left"
        )
        self.banner_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(
            self.banner, text="Use for all items", command=lambda: self.update_from_library(all_items=True)
        ).pack(side="right")
        ttk.Button(self.banner, text="Update this item", command=self.update_from_library).pack(
            side="right", padx=6
        )
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, pady=6)
        self.vtv = ttk.Treeview(body, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(body, command=self.vtv.yview)
        self.vtv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.vtv.pack(fill="both", expand=True)
        self.vtv.tag_configure("brand", font="SkBold")
        self.vtv.tag_configure("project", foreground=C["muted"])
        self.vtv.bind("<<TreeviewSelect>>", self._vehicle_selected)
        foot = ttk.Frame(tab)
        foot.pack(fill="x")
        self.credit = ttk.Label(foot, text="")
        self.credit.pack(side="left")
        ttk.Button(foot, text="Scan game…", command=self.app.scan_dialog).pack(side="right")
        self.edit_btn = ttk.Button(foot, text="Edit vehicle XML", command=self._edit_xml)
        self.edit_btn.pack(side="right", padx=6)
        self.save_btn = ttk.Button(foot, text="Save to library", command=self._save_to_library)
        self.save_btn.pack(side="right")
        Tooltip(self.save_btn, "Copy this vehicle definition into your library as an editable XML file.")

    @staticmethod
    def _vehicle_text(vid: str, v: vehlib.Vehicle, origin: str) -> str:
        """Tree text for one catalog entry: name, id, file, games, origin and accessory note."""
        extra = f"   ({v.id})"
        if vid != v.id and origin == "library":  # several library files define this id
            extra += f"   from {Path(v.source).name}"
        if v.game == "all":
            extra += "   · ATS + ETS2"
        return (
            v.display
            + extra
            + ("   - stored in this project" if origin == "project" else "")
            + ("" if v.accessories else "   · no accessories")
        )

    def render_vehicles(self) -> None:
        """Redraw the vehicle tree (filtered, for this game) and reselect the item's vehicle."""
        tv = self.vtv
        tv.delete(*tv.get_children())
        cat = self.app.vehicle_catalog()
        q = self.vfilter.get().strip().lower()
        game = self.app.project.game if self.app.project else ""
        brands: dict[tuple[str, str], str] = {}
        sel = self.app.catalog_key(self.item.vehicle()) if self.item and self.item.truck_id else ""
        hidden = 0
        kinds = sorted(
            cat.items(),
            key=lambda kv: (kv[1][0].kind, kv[1][0].brand.lower(), kv[1][0].display.lower()),
        )
        for vid, (v, origin) in kinds:
            if not v.fits_game(game):
                hidden += 1
                continue
            text = self._vehicle_text(vid, v, origin)
            if q and q not in text.lower() and q not in v.brand.lower():
                continue
            b = (v.kind, v.brand or "Miscellaneous")
            if b not in brands:
                label = b[1] + {"trailer": "   (trailers)", "bus": "   (coaches)"}.get(v.kind, "")
                brands[b] = tv.insert("", "end", text=label, open=True, tags=("brand",))
            tv.insert(brands[b], "end", iid=f"v::{vid}", text="   " + text, tags=(origin,))
        self._vehicle_notes(hidden, game)
        if sel and tv.exists(f"v::{sel}"):
            self._mute_select = True
            tv.selection_set(f"v::{sel}")
            tv.see(f"v::{sel}")
            self._mute_select = False
        self._update_truck_footer()

    def _vehicle_notes(self, hidden: int, game: str) -> None:
        """Grey notes at the end of the tree: other-game vehicles hidden, or how to get vehicles."""
        tv = self.vtv
        if hidden:
            other = "ATS" if game == "ets2" else "ETS2"
            tv.insert(
                "",
                "end",
                text=f"{hidden} {other} vehicle(s) hidden - this is a {game.upper()} project",
                tags=("project",),
            )
        if not tv.get_children():
            tv.insert(
                "",
                "end",
                text="No vehicles yet. Click 'Scan game…' and choose your game folder "
                "(it reads def.scs directly), or open an MS2 project that uses the truck.",
                tags=("project",),
            )

    def _vehicle_selected(self, _: object = None) -> None:
        """A vehicle was clicked: make it this skin's vehicle (and reload the tabs)."""
        if self._mute_select or self.loading or self.item is None:
            return
        sel = self.vtv.selection()
        if not sel or not sel[0].startswith("v::"):
            return
        key = sel[0][3:]
        v, _origin = self.app.vehicle_catalog()[key]
        cur = self.item.vehicle()
        if cur is not None and v.id == cur.id and v.same_content(cur):
            # re-selecting the current definition (also fired when the list is redrawn) changes nothing;
            # library updates go through the banner buttons so nothing changes silently
            return
        self.item.set_vehicle(v)
        self.app.changed(structure=True)
        self.load(self.item, keep_tab=True)

    def _update_truck_footer(self) -> None:
        """Footer credit line, the "library changed" banner and the Save to library button."""
        it = self.item
        v = it.vehicle() if it else None
        if v is None:
            self.credit.configure(text="Pick the truck or trailer this skin is for.")
            self.banner.pack_forget()
            return
        self.credit.configure(
            text=f"Vehicle XML definition provided by:   {v.definition_by or 'unknown'}"
            f"     ·  {len(v.variants)} {'bodies' if v.is_trailer else 'cabins'}, "
            f"{len(v.accessories)} accessories"
        )
        game = self.app.project.game if self.app.project else ""
        lib = self.app.library.match(v, game)
        if lib is not None and not lib.same_content(v):
            self.banner_lbl.configure(
                text=f"The definition of {v.id} in your vehicle library has changed "
                f"(edited {Path(lib.source).name}). Update this skin to use it?"
            )
            self.banner.pack(fill="x", pady=(6, 0), after=self.banner.master.winfo_children()[0])
        else:
            self.banner.pack_forget()
        self.save_btn.state(["!disabled"] if lib is None else ["disabled"])

    def update_from_library(self, all_items: bool = False) -> None:
        """Replace the vehicle stored in this item (or in every item using it) by the library's."""
        item = self._need_item()
        cur = item.vehicle()
        v = self.app.library.match(cur, self.app.project.game) if cur else None
        if v is None:
            return
        targets = [item]
        if all_items:
            targets = [
                i for i in self.app.project.items if isinstance(i, AdvancedTruckSkin) and i.truck_id == v.id
            ]
        for it in targets:
            it.set_vehicle(v)
        self.app.changed(structure=True)
        self.load(item, keep_tab=True)
        self.app.status(f"Updated {len(targets)} item(s) from {Path(v.source).name}")

    def _save_to_library(self) -> bool:
        """Copy the item's vehicle into the library as an XML file.

        Returns:
            True if it was saved; on an IO error a dialog is shown.
        """
        v = self.item.vehicle() if self.item else None
        if v is None:
            return False
        path = self.app.project.path
        try:
            p = vehlib.save_to_library(
                v, self.app.library_dir, comment=f"Saved from project {path.name if path else ''}"
            )
        except OSError as e:
            messagebox.showerror("Save to library", f"Could not save the vehicle file:\n{e}")
            return False
        self.app.reload_library()
        self.app.status(f"Saved {p.name} to your vehicle library")
        return True

    def _edit_xml(self) -> None:
        """Open the vehicle's library XML in the Vehicles window (saving it there first if needed)."""
        v = self.item.vehicle() if self.item else None
        if v is None:
            return
        lib = self.app.library.match(v, self.app.project.game if self.app.project else "")
        if lib is None:
            if not messagebox.askyesno(
                "Edit vehicle XML",
                "This vehicle is only stored inside the project.\n\n"
                "Save it to your vehicle library as an XML file and edit that?",
            ):
                return
            if not self._save_to_library():
                return
            lib = self.app.library.match(v)
        self.app.open_vehicle_manager(select=lib.key if lib else None)

    # ---------------------------------------------------------------- skin
    def _build_skin_tab(self) -> None:
        """Main tab: Basic, Icon, Metallic Options and Mask Options sections."""
        page = ScrollFrame(self)
        self.add(page, text="Main")
        self.skin_page = page
        bar = ttk.Frame(page.inner)
        bar.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Button(bar, text="Expand all", style="Tool.TButton", command=lambda: self._fold(True)).pack(
            side="right"
        )
        ttk.Button(bar, text="Collapse all", style="Tool.TButton", command=lambda: self._fold(False)).pack(
            side="right", padx=4
        )
        self.v: dict[str, tk.Variable] = {}
        self.widgets: dict[str, tk.Widget] = {}
        self.colors: dict[str, ColorRow] = {}
        self.main_sections: list[Section] = []
        self._build_basic_section()
        self._build_icon_section()
        self._build_windows_section()
        self._build_metallic_section()
        self._build_mask_section()
        ttk.Frame(page.inner, height=24).pack()

    # The _field helpers add to the section most recently opened with _section().
    def _section(self, title: str) -> None:
        """Start a collapsible section on the Main tab; the field helpers below add to it."""
        sec = Section(
            self.skin_page.inner, title, f"main.{title}", self.app.settings.setdefault("sections", {})
        )
        sec.pack(fill="x", padx=4, pady=(6, 0))
        self.main_sections.append(sec)
        self._body = sec.body
        self._form = Form(sec.body, 20)

    def _end_section(self) -> None:
        """Collect the section's problem marks."""
        self.marks.update(self._form.marks)

    def _entry_field(self, label: str, key: str, help: str | None = None) -> None:
        """Text field for item value ``key``."""
        var = tk.StringVar()
        self.v[key] = var
        _bind_entry(var, functools.partial(self._set, key), self)
        self._form.add(label, ttk.Entry(self._body, textvariable=var, width=40), key, help)

    def _spin_field(
        self,
        label: str,
        key: str,
        limits: tuple[float, float, float],
        help: str | None = None,
        fmt: str | None = None,
    ) -> None:
        """Number spinbox for ``key``; ``limits`` is (from, to, increment)."""
        var = tk.StringVar()
        self.v[key] = var
        _bind_entry(var, functools.partial(self._set_num, key), self)
        w = ttk.Frame(self._body)
        frm, to, inc = limits
        sp = ttk.Spinbox(
            w, from_=frm, to=to, increment=inc, textvariable=var, width=12, justify="right", format=fmt or ""
        )
        sp.pack(side="left")
        self._form.add(label, w, key, help, sticky="w")
        self.widgets[key] = sp

    def _check_field(
        self,
        label: str,
        key: str,
        help: str | None = None,
        cmd: Callable[[], None] | None = None,
    ) -> None:
        """Tick box for boolean ``key``; ``cmd`` runs after each click."""
        var = tk.BooleanVar()
        self.v[key] = var

        def clicked() -> None:
            self._set_bool(key, var.get())
            if cmd:
                cmd()

        cb = ttk.Checkbutton(self._body, text=label, variable=var, command=clicked)
        self._form.add("", cb, key, help, sticky="w")
        self.widgets[key] = cb

    def _color_field(self, label: str, key: str, help: str | None = None) -> None:
        """Linear colour row (with Select… picker) for ``key``."""
        row = ColorRow(self._body, on_change=functools.partial(self._set_color, key))
        self.colors[key] = row
        self._form.add(label, row, key, help)

    def _build_basic_section(self) -> None:
        """Internal name, name, price, unlock level, alternate UV and base colour."""
        self._section("Basic")
        self._entry_field(
            "Skin Internal Name",
            "InternalName",
            "Unique id of this skin inside the game: a-z, 0-9 and _, up to 10 characters.",
        )
        self._entry_field("Skin Name", "SkinName", help="Name shown in the truck shop.")
        self._spin_field("Skin Price", "SkinPrice", (0, 10_000_000, 100), "Price in the game currency.")
        self._spin_field("Unlock At Level", "SkinUnlock", (0, 100, 1))
        self._check_field(
            "Alternate UV",
            "AlternateUvSet",
            "Use the truck's alternate UV layout (only some trucks have one).",
        )
        self._color_field(
            "Base Color",
            "BaseColor",
            "Colour under transparent parts of the skin (game linear values 0-1). "
            "'Select' converts a normal colour for you.",
        )
        self._check_field(
            "Base Color Locked", "BaseColorLocked", "If unticked, players can change the base colour."
        )
        self._end_section()

    def _build_icon_section(self) -> None:
        """Shop icon path, Browse / Use skin image / Use default icon / overlay, and a preview."""
        self._section("Icon")
        g, f = self._body, self._form
        icon_row = ttk.Frame(g)
        self.icon = tk.StringVar()
        _bind_entry(self.icon, self._icon_typed, self)
        ttk.Entry(icon_row, textvariable=self.icon).pack(side="left", fill="x", expand=True)
        ttk.Button(icon_row, text="Browse", command=self._browse_icon).pack(side="left", padx=(6, 0))
        b = ttk.Button(icon_row, text="Use skin image", command=self._use_skin_image)
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, "Make the icon from this skin's first image.")
        b = ttk.Button(icon_row, text="Use default icon", command=lambda: self.icon.set(""))
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, DEFAULT_ICON_TIP)
        self.overlay_btn = ttk.Button(icon_row, text="Use overlay", command=self._browse_overlay)
        self.overlay_btn.pack(side="left", padx=(6, 0))
        Tooltip(
            self.overlay_btn, "Blend a second image (e.g. a logo) over the icon. Click again to remove it."
        )
        f.add("Path", icon_row, "SkinIcon", "Shop icon, shown at 256×64. Empty = the default icon.")
        self.icon_preview = Preview(g, self.app.thumbs, 256, 64)
        f.add("", self.icon_preview, None, sticky="w")
        self._end_section()

    def _icon_typed(self, value: str) -> None:
        """Icon path edited: store it, and refresh the preview once typing pauses."""
        self._set("SkinIcon", value)
        self._icon_soon()

    def _build_windows_section(self) -> None:
        """Coaches only: how the skin images are drawn, and painting the windows and trim.

        See :mod:`paintbooth.coach`. The controls are disabled for trucks and trailers.
        """
        self._section("Coaches")
        g, f = self._body, self._form
        self.win_on = tk.BooleanVar()
        self.uv_on = tk.BooleanVar()
        win = ttk.Checkbutton(
            g, text="Paint windows and gloss-black trim", variable=self.win_on, command=self._coach_toggled
        )
        f.add(
            "",
            win,
            "PaintWindows",
            "The windows (half see-through, also seen from inside) and the gloss-black parts show "
            "the skin image too, lined up with the paint. This replaces the coach's model while the "
            "mod is active, so every paint job of that body shows it.",
            sticky="w",
        )
        uv = ttk.Checkbutton(
            g,
            text="Images are drawn on the UV layout (advanced)",
            variable=self.uv_on,
            command=self._coach_toggled,
        )
        f.add(
            "",
            uv,
            "UvLayout",
            "Normally coach images are drawn on the 'blueprint' template (Templates\\ETS2\\Buses), which "
            "Paintbooth fits onto the coach. Tick this if your images use the game's own UV layout.",
            sticky="w",
        )
        self.coach_widgets: list[ttk.Widget] = [win, uv]
        self._end_section()

    def _coach_toggled(self) -> None:
        """One of the coach tick boxes changed."""
        if not self.loading and self.item is not None:
            self.item.set_extra("PaintWindows", "true" if self.win_on.get() else "false")
            self.item.set_extra("UvLayout", "true" if self.uv_on.get() else "false")
            self.app.changed()

    def _load_windows(self, it: AdvancedTruckSkin) -> None:
        """Show the coach options; they work only for coaches."""
        self.win_on.set(it.vars.bool("PaintWindows"))
        self.uv_on.set(it.vars.bool("UvLayout"))
        veh = it.vehicle()
        state = ["!disabled"] if veh is not None and veh.is_bus else ["disabled"]
        for w in self.coach_widgets:
            w.state(state)

    def _build_metallic_section(self) -> None:
        """Flip-flake paint settings (disabled unless Enable Metallic Color is ticked)."""
        self._section("Metallic Options")
        self._check_field(
            "Enable Metallic Color",
            "EnableMetallic",
            "Flip-flake (metallic / pearlescent) paint.",
            cmd=self._apply_enabled,
        )
        self._color_field("Flip Color", "FlipColor")
        self._color_field("Flake Color", "FlakeColor")
        self._spin_field("Flip Strength", "FlipStrenght", (0, 10, 0.01), fmt="%.2f")
        self._spin_field("Flake UV Scale", "FlakeUVScale", (0, 1000, 0.1), fmt="%.2f")
        self._spin_field("Flake Density", "FlakeDensity", (0, 100, 0.1), fmt="%.2f")
        self._spin_field("Flake Shininess", "FlakeShininess", (0, 1000, 0.1), fmt="%.2f")
        self._spin_field("Flake Clearcoat", "FlakeClearcoat", (0, 100, 0.1), fmt="%.2f")
        self._check_field("Flip Color Locked", "FlipColorLocked")
        self._check_field("Flake Color Locked", "FlakeColorLocked")
        self._end_section()

    def _build_mask_section(self) -> None:
        """Colour-mask settings (disabled unless Enable Mask Color is ticked)."""
        self._section("Mask Options")
        self._check_field(
            "Enable Mask Color",
            "EnableMask",
            "Colour mask mode: the red, green and blue channels of the skin are recoloured in game.",
            cmd=self._apply_enabled,
        )
        self._color_field("Red Mask Color", "RedColor")
        self._color_field("Green Mask Color", "GreenColor")
        self._color_field("Blue Mask Color", "BlueColor")
        self._check_field("Red Color Locked", "RedColorLocked")
        self._check_field("Green Color Locked", "GreenColorLocked")
        self._check_field("Blue Color Locked", "BlueColorLocked")
        self._end_section()

    def _fold(self, open_: bool) -> None:
        """Open or close every Main tab section."""
        for sec in self.main_sections:
            sec.set_open(open_)

    def _apply_enabled(self) -> None:
        """Grey out the metallic / mask fields whose Enable box is not ticked."""
        for group, flag in ((self.METALLIC, "EnableMetallic"), (self.MASK, "EnableMask")):
            on = bool(self.v[flag].get())
            for k in group:
                if k in self.colors:
                    self.colors[k].state(on)
                elif k in self.widgets:
                    self.widgets[k].state(["!disabled"] if on else ["disabled"])  # type: ignore[attr-defined]

    # ---------------------------------------------------------------- load
    def load(self, item: AdvancedTruckSkin, keep_tab: bool = False) -> None:
        """Fill every tab from ``item``.

        Args:
            item: The skin to edit.
            keep_tab: Accepted for callers' clarity; the selected tab is never changed here.
        """
        self.loading = True
        self.item = item
        self._load_values(item)
        self.loading = False
        self._apply_enabled()
        self._icon_soon.cancel()
        self._icon_preview()
        self.render_vehicles()
        for arr in self.blocks:
            self.rebuild_blocks(arr)

    def _load_values(self, it: AdvancedTruckSkin) -> None:
        """Copy the item's Main tab values into the widgets."""
        for k in ("InternalName", "SkinName"):
            self.v[k].set(it.get(k))
        for k in self.INT_FIELDS + self.FLOAT_FIELDS:
            raw = it.get(k, "0") or "0"
            if k in self.INT_FIELDS:
                self.v[k].set(raw)
            else:
                try:
                    self.v[k].set(f"{float(raw):.2f}")
                except ValueError:
                    self.v[k].set(raw)
        for k in self.BOOL_FIELDS:
            # MS2's defaults: the three mask colours are locked unless the file says otherwise
            self.v[k].set(it.vars.bool(k, k.endswith("Locked") and k.startswith(("Red", "Green", "Blue"))))
        for k, row in self.colors.items():
            row.set(it.vars.color(k))
        self.icon.set(it.get("SkinIcon"))
        self._load_windows(it)

    @staticmethod
    def _image_options(array: str, veh: vehlib.Vehicle | None) -> list[Option]:
        """What the image blocks of ``array`` can tick: the cabins/bodies, or the accessories."""
        if veh is None:
            return []
        if array == "CabVariantSkin":
            return [
                (x.id, f"{x.name}  -  {x.description}" if x.description else x.name, "") for x in veh.variants
            ]
        return [(a.value, a.name, a.group) for a in veh.accessories]

    def rebuild_blocks(self, array: str) -> None:
        """Recreate the image blocks of one tab, and its hint, overview and template bar."""
        item = self._need_item()
        page = self.pages[array]
        for b in self.blocks[array]:
            b.destroy()
        self.blocks[array] = []
        veh = item.vehicle()
        opts = self._image_options(array, veh)
        page.info_lbl.configure(text=self._tab_hint(array, veh), style="Muted.TLabel")
        if array == "AccessorySkins":
            self._show_overview(page, veh, opts, has_images=bool(item.entries(array)))
            if veh is not None and not veh.accessories:
                page.info_lbl.configure(style="Warn.TLabel")
        else:
            self.tpl_bar.refresh()
        for i, e in enumerate(item.entries(array)):
            b = ImageBlock(page.holder, self, array, i)
            b.pack(fill="x", pady=(0, 10))
            b.load(e, opts)
            self.blocks[array].append(b)
        self.app.revalidate_soon()

    @staticmethod
    def _show_overview(
        page: ImagesPage, veh: vehlib.Vehicle | None, opts: list[Option], has_images: bool
    ) -> None:
        """Accessories tab: list the accessories to tick until the first image exists."""
        if veh is not None and veh.accessories and not has_images:
            # keep ticks that still exist on the (possibly changed) vehicle
            page.pick.set_items(opts, [v for v in page.pick.selected if any(o[0] == v for o in opts)])
            page.overview.pack(fill="x", padx=8, pady=(8, 0), before=page.holder)
            page.add_btn.configure(text=" Add image for the ticked accessories")
        else:
            page.overview.pack_forget()
            page.add_btn.configure(text=" Add image")

    def _tab_hint(self, array: str, veh: vehlib.Vehicle | None) -> str:
        """Help text above the images of a tab ("" once there are images and nothing is wrong)."""
        if veh is None:
            return "Choose the truck or trailer on the Vehicle tab first."
        n_img = len(self._need_item().entries(array))
        if array == "CabVariantSkin":
            kind = "bodies" if veh.is_trailer else "cabins"
            return (
                ""
                if n_img
                else (
                    f"{veh.name or veh.id} has {len(veh.variants)} {kind}. Click Add image, then tick "
                    f"the {kind} the image is for."
                )
            )
        if not veh.accessories:
            lib = self.app.library.match(veh)
            where = f" ({Path(lib.source).name})" if lib else ""
            return (
                f"This definition of {veh.id}{where} lists no paintable accessories, so there is nothing "
                "to tick here. Pick a different definition on the Vehicle tab (entries marked "
                "'no accessories' have none), or add accessories in Home > Vehicles > Add accessory…."
            )
        groups = len({a.group for a in veh.accessories})
        return (
            ""
            if n_img
            else (
                f"{veh.name or veh.id} has {len(veh.accessories)} paintable accessories in {groups} "
                "groups (listed below). Tick the ones one image should paint, then click 'Add image "
                "for the ticked accessories' or 'Generate color'. You can change the ticks later."
            )
        )

    def _require_vehicle(self, title: str) -> bool:
        """True if the item has a vehicle; otherwise say so and switch to the Vehicle tab."""
        if self._need_item().truck_id:
            return True
        messagebox.showinfo(title, "Choose the truck or trailer first (Vehicle tab).")
        self.select(0)
        return False

    def _scroll_to_end(self, array: str) -> None:
        """Scroll a tab to its last image (just added)."""
        canvas = self.pages[array].canvas
        canvas.update_idletasks()
        canvas.yview_moveto(1.0)

    def add_image(self, array: str) -> None:
        """Add an empty image to ``array`` and put the cursor in its path field."""
        if self.item is None or not self._require_vehicle("Add image"):
            return
        ticked = self._ticked() if array == "AccessorySkins" else []
        e = self.item.add_entry(array)
        if ticked:
            e.suitable = ticked
        veh = self.item.vehicle()
        if array == "CabVariantSkin" and not self.item.entries(array)[:-1] and veh is not None:
            e.suitable = [x.id for x in veh.variants]  # first image: all cabins, like MS2
        self.app.changed()
        self.rebuild_blocks(array)
        self._scroll_to_end(array)
        self.blocks[array][-1].focus_path()

    def _ticked(self) -> list[str]:
        """Accessories ticked in the overview list (only shown before the first accessory image)."""
        page = self.pages["AccessorySkins"]
        if page.overview.winfo_manager():
            sel = list(page.pick.selected)
            page.pick.selected = []
            return sel
        return []

    def add_color(self) -> None:
        """Generate a colour swatch and add it as an accessory image for the ticked accessories."""
        if self.item is None or not self._require_vehicle("Generate color"):
            return
        from .colorgen import ColorGenerator

        p = ColorGenerator(self.app).run()
        if not p:
            return
        ticked = self._ticked()
        e = self.item.add_entry("AccessorySkins")
        e.image = str(p)
        if ticked:
            e.suitable = ticked
        self.app.changed()
        self.rebuild_blocks("AccessorySkins")
        self._scroll_to_end("AccessorySkins")

    # ---------------------------------------------------------------- setters
    def _set(self, key: str, value: str) -> None:
        """Write a text value; the names also change the item's label in the list."""
        if self.loading or self.item is None:
            return
        self.item.set(key, value)
        self.app.changed(label=key in ("InternalName", "SkinName"))

    def _set_num(self, key: str, value: str) -> None:
        """Write a number typed with either decimal separator; incomplete input is not stored.

        Prices and levels are stored as integers; the flake settings without a trailing ``.0``.
        """
        if self.loading or self.item is None:
            return
        v = value.strip().replace(",", ".")
        try:
            f = float(v)
        except ValueError:
            return
        if not math.isfinite(f):  # "nan" / "inf" parse as floats but are no use to the game
            return
        # prices and levels are whole numbers; the rest drop a trailing ".0" (MS2 writes "1", not "1.0")
        v = str(int(f)) if key in self.INT_FIELDS or f == int(f) else f"{f:g}"
        self.item.set(key, v)
        self.app.changed()

    def _set_bool(self, key: str, value: bool) -> None:
        """Write a boolean value."""
        if self.loading or self.item is None:
            return
        self.item.vars.set_bool(key, bool(value))
        self.app.changed()

    def _set_color(self, key: str, rgb: tuple[float, float, float]) -> None:
        """Write a linear colour."""
        if self.loading or self.item is None:
            return
        self.item.vars.set_color(key, rgb)
        self.app.changed()

    def _browse_icon(self) -> None:
        """Pick the shop icon image."""
        p = filedialog.askopenfilename(
            title="Skin icon",
            initialdir=self.app.image_dir(),
            filetypes=[("Images", "*.png *.dds *.jpg"), ("All files", "*.*")],
        )
        if p:
            self.icon.set(str(Path(p)))

    def _browse_overlay(self) -> None:
        """Toggle the icon overlay: remove it if set, else pick a PNG to blend over the icon."""
        if self.item is None:
            return
        if self.item.vars.child_value("SkinIcon", "OverlaySource"):
            self.item.vars.set_child_value("SkinIcon", "OverlaySource", "")
        else:
            p = filedialog.askopenfilename(
                title="Overlay image", filetypes=[("Images", "*.png"), ("All files", "*.*")]
            )
            if not p:
                return
            self.item.vars.set_child_value("SkinIcon", "OverlaySource", str(Path(p)))
        self.app.changed()
        self._icon_preview()

    def _use_skin_image(self) -> None:
        """Use this skin's first image that exists as the icon."""
        if self.item is None:
            return
        for e in self.item.entries("CabVariantSkin"):
            p = resolve(e.image, self.app.project_dir())
            if p is not None:
                self.icon.set(str(p))
                return
        messagebox.showinfo("Skin icon", "Add a skin image on the Skins tab first.")

    def _icon_preview(self) -> None:
        """Show the shop icon (or the default) and the overlay button's current action."""
        if self.item is None:
            return
        p = resolve(self.icon.get(), self.app.project_dir()) or self.app.default_skin_icon()
        has_ov = bool(self.item.vars.child_value("SkinIcon", "OverlaySource"))
        self.overlay_btn.configure(text="Remove overlay" if has_ov else "Use overlay")
        self.icon_preview.show(p, "No icon")

    # ---------------------------------------------------------------- problems
    def show_problems(self, probs: list[Any]) -> None:
        """Set the field and image marks from the item's ``Problem``s, and flag tabs with errors.

        Image problems have fields like ``CabVariantSkin[2]`` (the third skin image).
        """
        _set_marks(self.marks, probs, ok_fields=("InternalName", "SkinName", "Truck"))
        for arr, blocks in self.blocks.items():
            for k, b in enumerate(blocks):
                mine = [p for p in probs if p.field == f"{arr}[{k}]"]
                if mine:
                    b.mark.set(_worst(mine), "\n".join(p.message for p in mine))
                else:
                    b.mark.set("ok")
        n_err = {
            arr: sum(1 for p in probs if p.field.startswith(arr) and p.level == "error")
            for arr in self.blocks
        }
        for i, (arr, title) in enumerate(self.IMAGE_TABS):
            # tabs 2 and 3: Vehicle and Main come first
            self.tab(
                2 + i, text=title, image=icons().get("mark_err", 12) if n_err[arr] else "", compound="right"
            )
