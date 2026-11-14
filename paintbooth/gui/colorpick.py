"""Colour picker used everywhere in the app.

* Saturation/brightness square + hue strip, hex, RGB and SCS (linear) values
* Palettes to pick from (basic, fleet colours, RAL selection, material, greys) and your own palettes
* Saved colours and recent colours, kept in the settings file, so they're shared by all projects

Where the shared colours live: every class and function here takes the settings dict (or the
``App``) explicitly where it can. ``widgets.ColorRow`` has no access to the app, so ``App`` also
registers itself once with :func:`configure`; :func:`ask_color` and the functions without a
``settings`` argument then use that app's settings. Before ``configure`` is called (tests, the CLI),
they use a fresh throw-away dict, so nothing is written to the real settings, and :func:`ask_color`
falls back to Tk's own colour chooser.
"""

from __future__ import annotations

import colorsys
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import Any

from PIL import Image, ImageChops, ImageTk

from ..util import linear_to_srgb, srgb_to_linear
from .theme import C
from .widgets import Tooltip
from .winutil import tool_window

RGB = tuple[int, int, int]  # 0-255 sRGB
Settings = dict[str, Any]

SQ = 220  # saturation/value square size
HUE_W = 22  # width of the hue strip
SW = 20  # palette swatch size
MAX_SAVED = 48
MAX_RECENT = 16


class _Registry:
    """Holds the app registered with :func:`configure` (one per process: there is one main window)."""

    app: Any = None


_REGISTRY = _Registry()


def configure(app: Any) -> None:
    """Register the main window, so colour rows (which have no app) reach the shared settings.

    ``App.__init__`` calls this once. Calling it again replaces the app (a new main window).
    """
    _REGISTRY.app = app


def _hex(rgb: Sequence[float]) -> str:
    """``(255, 128, 0)`` -> ``"#ff8000"``."""
    return "#" + "".join(f"{int(c):02x}" for c in rgb)


def _rgb(h: str) -> RGB:
    """``"#ff8000"`` or ``"f80"`` -> ``(255, 128, 0)``.

    Raises:
        ValueError: Not 3 or 6 hex digits.
    """
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


PALETTES: dict[str, list[tuple[str, str]]] = {
    "Basic": [
        ("White", "#ffffff"),
        ("Light grey", "#c8c8c8"),
        ("Grey", "#808080"),
        ("Dark grey", "#404040"),
        ("Black", "#000000"),
        ("Red", "#e01b1b"),
        ("Dark red", "#8b0000"),
        ("Orange", "#ff7f00"),
        ("Amber", "#ffbf00"),
        ("Yellow", "#ffe000"),
        ("Lime", "#9be000"),
        ("Green", "#1fa83a"),
        ("Dark green", "#0b5d1e"),
        ("Teal", "#008080"),
        ("Cyan", "#00c8e0"),
        ("Sky blue", "#4aa8ff"),
        ("Blue", "#1f4fd8"),
        ("Navy", "#0b1f5c"),
        ("Purple", "#7a2bd0"),
        ("Magenta", "#d01fa0"),
        ("Pink", "#ff7fbf"),
        ("Brown", "#7a4a1f"),
        ("Beige", "#e3d3b0"),
        ("Gold", "#c9a227"),
    ],
    "Fleet colours": [
        ("Fleet white", "#f2f2ee"),
        ("Cream", "#efe6cf"),
        ("Silver", "#b7bbbf"),
        ("Gunmetal", "#4b5157"),
        ("Graphite", "#2c3034"),
        ("Gloss black", "#0d0d0f"),
        ("Fire red", "#c41a16"),
        ("Burgundy", "#6e1423"),
        ("Tangerine", "#f2701d"),
        ("School-bus yellow", "#f6b800"),
        ("Lemon", "#f4e04d"),
        ("British racing green", "#004225"),
        ("Forest green", "#2e5b2e"),
        ("Kelly green", "#3a9b3a"),
        ("Petrol", "#005f6a"),
        ("Turquoise", "#1fb5ad"),
        ("Royal blue", "#1d3fa8"),
        ("Cobalt", "#0047ab"),
        ("Midnight blue", "#14213d"),
        ("Baby blue", "#8fc1e8"),
        ("Plum", "#5b2a5e"),
        ("Chocolate", "#4a2c1d"),
        ("Sand", "#c8ad7f"),
        ("Copper", "#b06a3b"),
    ],
    "RAL selection (approx.)": [
        ("RAL 1003 Signal yellow", "#f9a800"),
        ("RAL 1015 Light ivory", "#e6d2b5"),
        ("RAL 1023 Traffic yellow", "#f7b500"),
        ("RAL 2004 Pure orange", "#e25303"),
        ("RAL 2008 Bright red orange", "#ed6b21"),
        ("RAL 2011 Deep orange", "#ec7c25"),
        ("RAL 3000 Flame red", "#a72920"),
        ("RAL 3002 Carmine red", "#9b2423"),
        ("RAL 3003 Ruby red", "#861a22"),
        ("RAL 3020 Traffic red", "#bb1e10"),
        ("RAL 4005 Blue lilac", "#6c4675"),
        ("RAL 5002 Ultramarine blue", "#20214f"),
        ("RAL 5005 Signal blue", "#154889"),
        ("RAL 5012 Light blue", "#0089b6"),
        ("RAL 5017 Traffic blue", "#005b8c"),
        ("RAL 5022 Night blue", "#222d5a"),
        ("RAL 6001 Emerald green", "#366735"),
        ("RAL 6002 Leaf green", "#325928"),
        ("RAL 6005 Moss green", "#114232"),
        ("RAL 6018 Yellow green", "#48a43f"),
        ("RAL 6024 Traffic green", "#008754"),
        ("RAL 6029 Mint green", "#006f3d"),
        ("RAL 7016 Anthracite grey", "#383e42"),
        ("RAL 7021 Black grey", "#2f3234"),
        ("RAL 7035 Light grey", "#cbd0cc"),
        ("RAL 7040 Window grey", "#9da3a6"),
        ("RAL 7042 Traffic grey A", "#8f9695"),
        ("RAL 7043 Traffic grey B", "#4e5451"),
        ("RAL 9001 Cream", "#e9e0d2"),
        ("RAL 9003 Signal white", "#ecece7"),
        ("RAL 9005 Jet black", "#0a0a0d"),
        ("RAL 9006 White aluminium", "#a1a1a0"),
        ("RAL 9007 Grey aluminium", "#878581"),
        ("RAL 9010 Pure white", "#f1ece1"),
        ("RAL 9016 Traffic white", "#f1f0ea"),
        ("RAL 9017 Traffic black", "#2a292a"),
    ],
    "Material": [
        ("Red", "#f44336"),
        ("Pink", "#e91e63"),
        ("Purple", "#9c27b0"),
        ("Deep purple", "#673ab7"),
        ("Indigo", "#3f51b5"),
        ("Blue", "#2196f3"),
        ("Light blue", "#03a9f4"),
        ("Cyan", "#00bcd4"),
        ("Teal", "#009688"),
        ("Green", "#4caf50"),
        ("Light green", "#8bc34a"),
        ("Lime", "#cddc39"),
        ("Yellow", "#ffeb3b"),
        ("Amber", "#ffc107"),
        ("Orange", "#ff9800"),
        ("Deep orange", "#ff5722"),
        ("Brown", "#795548"),
        ("Grey", "#9e9e9e"),
        ("Blue grey", "#607d8b"),
        ("Red 900", "#b71c1c"),
        ("Blue 900", "#0d47a1"),
        ("Green 900", "#1b5e20"),
        ("Orange 900", "#e65100"),
        ("Grey 900", "#212121"),
    ],
    "Greys": [
        (f"{v}", _hex((v, v, v)))
        for v in (255, 238, 221, 204, 187, 170, 153, 136, 119, 102, 85, 68, 51, 34, 17, 0)
    ],
}


def _settings(settings: Settings | None = None) -> Settings:
    """``settings`` if given, else the registered app's, else a throw-away dict (see module doc)."""
    if settings is not None:
        return settings
    app = _REGISTRY.app
    return app.settings if app is not None else {}


def saved_colors(settings: Settings | None = None) -> list[str]:
    """The user's saved colours as ``"#rrggbb"``, oldest first."""
    return list(_settings(settings).get("saved_colors", []))


def recent_colors(settings: Settings | None = None) -> list[str]:
    """Recently picked colours as ``"#rrggbb"``, newest first."""
    return list(_settings(settings).get("recent_colors", []))


def remember_recent(rgb: Sequence[int], settings: Settings | None = None) -> None:
    """Put ``rgb`` first in the recent colours (each colour once, at most :data:`MAX_RECENT`)."""
    s = _settings(settings)
    h = _hex(rgb)
    lst = [c for c in s.get("recent_colors", []) if c != h]
    s["recent_colors"] = [h, *lst[: MAX_RECENT - 1]]


class Swatches(tk.Canvas):
    """A wrapping grid of colour swatches. Click = pick, double-click = pick and OK."""

    def __init__(
        self,
        master: tk.Misc,
        on_pick: Callable[[RGB], object],
        on_double: Callable[[RGB], object] | None = None,
        on_remove: Callable[[str], object] | None = None,
        cols: int = 12,
        on_rename: Callable[[str], object] | None = None,
    ):
        super().__init__(master, highlightthickness=0, bd=0, background=C["bg"], height=SW + 4)
        self.on_pick, self.on_double, self.on_remove, self.on_rename = (
            on_pick,
            on_double,
            on_remove,
            on_rename,
        )
        # right-click menu: lists with on_rename (user palettes) turn it on via .editable themselves
        self.editable = on_remove is not None and on_rename is None
        self.cols = cols
        self.items: list[tuple[str, str]] = []  # (name, "#rrggbb")
        self._tip: tk.Toplevel | None = None
        self.bind("<Button-1>", lambda e: self._hit(e, self.on_pick))
        self.bind("<Double-Button-1>", lambda e: self._hit(e, self.on_double))
        self.bind("<Button-3>", self._menu)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self._hide_tip())

    def show(self, items: Sequence[tuple[str, str]]) -> None:
        """Draw ``items`` (``(name, "#rrggbb")`` pairs) and resize to fit them."""
        self.items = list(items)
        self.delete("all")
        for i, (_name, h) in enumerate(items):
            r, c = divmod(i, self.cols)
            x, y = 2 + c * (SW + 4), 2 + r * (SW + 4)
            self.create_rectangle(x, y, x + SW, y + SW, fill=h, outline=C["border"])
        rows = max(1, (len(items) + self.cols - 1) // self.cols)
        self.configure(height=rows * (SW + 4) + 2, width=self.cols * (SW + 4) + 2)

    def _index(self, e: tk.Event) -> int | None:
        """Index of the swatch under the mouse, or None."""
        c, r = (e.x - 2) // (SW + 4), (e.y - 2) // (SW + 4)
        i = r * self.cols + c
        return i if 0 <= c < self.cols and 0 <= i < len(self.items) else None

    def _hit(self, e: tk.Event, fn: Callable[[RGB], object] | None) -> None:
        """Call ``fn`` with the colour under the mouse."""
        i = self._index(e)
        if i is not None and fn:
            fn(_rgb(self.items[i][1]))

    def _menu(self, e: tk.Event) -> None:
        """Right-click: Rename… (user palettes) and Remove."""
        i = self._index(e)
        on_remove, on_rename = self.on_remove, self.on_rename
        if i is None or not on_remove or not self.editable:
            return
        h = self.items[i][1]
        m = tk.Menu(self, tearoff=0)
        if on_rename:
            m.add_command(label="Rename…", command=lambda: on_rename(h))
        m.add_command(label=f"Remove {h}", command=lambda: on_remove(h))
        m.tk_popup(e.x_root, e.y_root)

    def _hover(self, e: tk.Event) -> None:
        """Show the swatch's name and hex value in a small tooltip window."""
        i = self._index(e)
        if i is None:
            self._hide_tip()
            return
        name, h = self.items[i]
        text = h if name == h else f"{name}  {h}"
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            self._lbl = tk.Label(
                self._tip, background=C["raised"], foreground=C["fg"], padx=6, pady=2, relief="solid", bd=1
            )
            self._lbl.pack()
        self._lbl.configure(text=text)
        self._tip.geometry(f"+{e.x_root + 12}+{e.y_root + 14}")

    def _hide_tip(self) -> None:
        """Remove the hover tooltip."""
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class ColorPanel(ttk.Frame):
    """Everything except the dialog buttons; used by the picker and by Generate color.

    The colour is kept as HSV (``h``, ``s``, ``v`` in 0-1) so dragging in the square doesn't lose
    the hue at grey; a colour typed as hex/RGB/linear is kept exactly in ``_exact`` for one update,
    so rounding through HSV never changes what the user typed.

    Args:
        master: Parent widget.
        initial: Starting colour (0-255); also shown as the "before" half of the preview.
        on_change: Called with the new ``(r, g, b)`` on every change.
        on_double: Called with the colour when a swatch is double-clicked (the picker's "OK").
        settings: Where palettes, saved and recent colours live; default: the registered app's.
    """

    def __init__(
        self,
        master: tk.Misc,
        initial: Sequence[int] = (255, 255, 255),
        on_change: Callable[[RGB], object] | None = None,
        on_double: Callable[[RGB], object] | None = None,
        settings: Settings | None = None,
    ):
        super().__init__(master)
        self.on_change = on_change
        self.settings = _settings(settings)
        self._mute = False  # True while _update() writes the fields: their callbacks must not fire
        self._exact: RGB | None = None
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in initial))
        self.initial = tuple(initial)
        self._build_picker()
        self._build_values()
        self._build_lists(on_double)
        self._show_palette()
        self._show_saved()
        self.recent.show([(h, h) for h in recent_colors(self.settings)])
        self._update(None)

    def _build_picker(self) -> None:
        """Saturation/value square and the hue strip."""
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nw")
        self.sq = tk.Canvas(
            left,
            width=SQ,
            height=SQ,
            highlightthickness=1,
            highlightbackground=C["border"],
            cursor="crosshair",
        )
        self.sq.grid(row=0, column=0)
        self.hue = tk.Canvas(
            left,
            width=HUE_W,
            height=SQ,
            highlightthickness=1,
            highlightbackground=C["border"],
            cursor="sb_v_double_arrow",
        )
        self.hue.grid(row=0, column=1, padx=(8, 0))
        hue_img = Image.new("RGB", (1, SQ))
        for y in range(SQ):
            hue_img.putpixel((0, y), tuple(round(c * 255) for c in colorsys.hsv_to_rgb(y / SQ, 1, 1)))
        self._hue_photo = ImageTk.PhotoImage(hue_img.resize((HUE_W, SQ)))
        self.hue.create_image(0, 0, anchor="nw", image=self._hue_photo)
        self._hue_mark = self.hue.create_rectangle(0, 0, HUE_W, 3, outline="#ffffff", width=2)
        self._sq_img = self.sq.create_image(0, 0, anchor="nw")
        self._sq_mark = self.sq.create_oval(0, 0, 10, 10, outline="#ffffff", width=2)
        for seq in ("<Button-1>", "<B1-Motion>"):
            self.sq.bind(seq, self._sq_click)
            self.hue.bind(seq, self._hue_click)

    def _build_values(self) -> None:
        """Preview swatch and the hex, RGB, SCS (linear) and HSV fields."""
        vals = ttk.Frame(self)
        vals.grid(row=0, column=1, sticky="nw", padx=(16, 0))
        self.prev = tk.Canvas(
            vals, width=120, height=44, highlightthickness=1, highlightbackground=C["border"]
        )
        self.prev.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        self.prev.create_text(4, 2, anchor="nw", text="", tags="lbl")
        ttk.Label(vals, text="Hex").grid(row=1, column=0, sticky="e", padx=(0, 6))
        self.hex = tk.StringVar()
        e = ttk.Entry(vals, textvariable=self.hex, width=10)
        e.grid(row=1, column=1, columnspan=3, sticky="w", pady=2)
        e.bind("<KeyRelease>", lambda ev: self._from_hex())
        e.bind("<FocusOut>", lambda ev: self._from_hex())
        ttk.Label(vals, text="R G B").grid(row=2, column=0, sticky="e", padx=(0, 6))
        self.rgbv = [tk.StringVar() for _ in range(3)]
        for i, v in enumerate(self.rgbv):
            sp = ttk.Spinbox(
                vals,
                from_=0,
                to=255,
                increment=1,
                textvariable=v,
                width=5,
                justify="right",
                command=self._from_rgb,
            )
            sp.grid(row=2, column=1 + i, sticky="w", pady=2, padx=(0, 4))
            sp.bind("<KeyRelease>", lambda ev: self._from_rgb())
        ttk.Label(vals, text="SCS").grid(row=3, column=0, sticky="e", padx=(0, 6))
        self.linv = [tk.StringVar() for _ in range(3)]
        for i, v in enumerate(self.linv):
            en = ttk.Entry(vals, textvariable=v, width=7, justify="right")
            en.grid(row=3, column=1 + i, sticky="w", pady=2, padx=(0, 4))
            en.bind("<KeyRelease>", lambda ev: self._from_lin())
            en.bind("<FocusOut>", lambda ev: self._from_lin())
        Tooltip(
            vals.grid_slaves(row=3, column=0)[0], "Linear values (0-1), as written in the game's .sii files"
        )
        ttk.Label(vals, text="H S V").grid(row=4, column=0, sticky="e", padx=(0, 6))
        self.hsv_lbl = ttk.Label(vals, text="", style="Muted.TLabel")
        self.hsv_lbl.grid(row=4, column=1, columnspan=3, sticky="w", pady=2)

    def _build_lists(self, on_double: Callable[[RGB], object] | None) -> None:
        """Palette chooser and swatches, saved colours and recent colours."""
        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="Palette").grid(row=0, column=0, sticky="w")
        self.pal_var = tk.StringVar()
        prow = ttk.Frame(bottom)
        prow.grid(row=0, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self.pal_cb = ttk.Combobox(prow, textvariable=self.pal_var, state="readonly", width=26)
        self.pal_cb.pack(side="left")
        self.pal_cb.bind("<<ComboboxSelected>>", lambda e: self._show_palette())
        ttk.Button(prow, text="New palette…", command=self._new_palette).pack(side="left", padx=(8, 0))
        self.pal_edit = ttk.Frame(prow)
        ttk.Button(self.pal_edit, text="+ Add current colour", command=self._add_to_palette).pack(side="left")
        ttk.Button(self.pal_edit, text="Rename…", command=self._rename_palette).pack(side="left", padx=(6, 0))
        ttk.Button(self.pal_edit, text="Delete palette", command=self._delete_palette).pack(
            side="left", padx=(6, 0)
        )
        self.pal = Swatches(
            bottom, self._pick, on_double, on_remove=self._remove_from_palette, on_rename=self._rename_colour
        )
        self.pal.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.pal_hint = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.pal_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self._palette_names(select=self.settings.get("palette"))

        sv = ttk.Frame(bottom)
        sv.grid(row=3, column=0, columnspan=3, sticky="ew")
        ttk.Label(sv, text="Saved colours").pack(side="left")
        ttk.Button(sv, text="+ Save current", command=self.save_current).pack(side="left", padx=(8, 0))
        ttk.Label(sv, text="right-click a swatch to remove it", style="Muted.TLabel").pack(
            side="left", padx=(8, 0)
        )
        self.saved = Swatches(bottom, self._pick, on_double, on_remove=self._remove_saved)
        self.saved.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 8))
        ttk.Label(bottom, text="Recent").grid(row=5, column=0, sticky="w")
        self.recent = Swatches(bottom, self._pick, on_double)
        self.recent.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------ lists
    # palettes: built-in ones are fixed; your own live in the settings (shared by all projects)
    def _user_palettes(self) -> dict[str, list[list[str]]]:
        """The user's palettes: name -> list of ``[name, "#rrggbb"]`` (lists, as stored in JSON)."""
        return self.settings.setdefault("user_palettes", {})

    def _palette_names(self, select: str | None = None) -> None:
        """Fill the palette drop-down and select ``select`` (or the first palette)."""
        names = list(PALETTES) + list(self._user_palettes())
        self.pal_cb.configure(values=names)
        self.pal_var.set(select if select in names else names[0])
        self._show_palette()

    def _is_user(self) -> bool:
        """True if the selected palette is one of the user's (editable) palettes."""
        return self.pal_var.get() in self._user_palettes()

    def _show_palette(self) -> None:
        """Show the selected palette's swatches (and the edit buttons for a user palette)."""
        name = self.pal_var.get()
        self.settings["palette"] = name
        if self._is_user():
            cols = [(c[0], c[1]) for c in self._user_palettes()[name]]
            self.pal.editable = True
            self.pal.show(cols)
            self.pal_edit.pack(side="left", padx=(12, 0))
            self.pal_hint.configure(
                text="Your palette: right-click a colour to rename or remove it."
                if cols
                else "Empty palette: pick a colour above and click '+ Add current colour'."
            )
        else:
            self.pal.editable = False
            self.pal.show(PALETTES[name])
            self.pal_edit.pack_forget()
            self.pal_hint.configure(text="")

    def _ask(self, title: str, prompt: str, initial: str = "") -> str | None:
        """Ask for a line of text (None if cancelled)."""
        from tkinter import simpledialog

        return simpledialog.askstring(title, prompt, initialvalue=initial, parent=self.winfo_toplevel())

    def _new_palette(self) -> None:
        """Create an empty user palette with a unique name."""
        name = (self._ask("New palette", "Name of the new palette:") or "").strip()
        if not name:
            return
        while name in PALETTES or name in self._user_palettes():
            name += " (2)" if not name.endswith(")") else "'"
        self._user_palettes()[name] = []
        self._palette_names(select=name)

    def _rename_palette(self) -> None:
        """Rename the selected user palette (keeping its position in the list)."""
        old = self.pal_var.get()
        new = (self._ask("Rename palette", "New name:", old) or "").strip()
        if not new or new == old or new in PALETTES or new in self._user_palettes():
            return
        pals = self._user_palettes()
        self.settings["user_palettes"] = {(new if k == old else k): v for k, v in pals.items()}
        self._palette_names(select=new)

    def _delete_palette(self) -> None:
        """Delete the selected user palette after asking."""
        from tkinter import messagebox

        name = self.pal_var.get()
        if messagebox.askyesno(
            "Delete palette", f"Delete the palette '{name}'?", parent=self.winfo_toplevel()
        ):
            self._user_palettes().pop(name, None)
            self._palette_names()

    def _add_to_palette(self) -> None:
        """Add the current colour to the selected user palette (once)."""
        h = _hex(self.current())
        cols = self._user_palettes()[self.pal_var.get()]
        if not any(c[1] == h for c in cols):
            cols.append([h, h])
        self._show_palette()

    def _remove_from_palette(self, h: str) -> None:
        """Remove colour ``h`` from the selected user palette."""
        if self._is_user():
            pal = self._user_palettes()[self.pal_var.get()]
            pal[:] = [c for c in pal if c[1] != h]
            self._show_palette()

    def _rename_colour(self, h: str) -> None:
        """Give colour ``h`` of the selected user palette a name (empty = show the hex value)."""
        if not self._is_user():
            return
        pal = self._user_palettes()[self.pal_var.get()]
        for c in pal:
            if c[1] == h:
                new = (
                    self._ask("Colour name", "Name for this colour:", "" if c[0] == h else c[0]) or ""
                ).strip()
                c[0] = new or h
        self._show_palette()

    def _show_saved(self) -> None:
        """Redraw the saved colours."""
        lst = saved_colors(self.settings)
        self.saved.show([(h, h) for h in lst])

    def save_current(self) -> None:
        """Add the current colour to the saved colours (moved to the end if already there)."""
        s = self.settings
        h = _hex(self.current())
        lst = [c for c in s.get("saved_colors", []) if c != h]
        s["saved_colors"] = [*lst, h][-MAX_SAVED:]
        self._show_saved()

    def _remove_saved(self, h: str) -> None:
        """Remove ``h`` from the saved colours."""
        s = self.settings
        s["saved_colors"] = [c for c in s.get("saved_colors", []) if c != h]
        self._show_saved()

    # ------------------------------------------------------------ values
    def get(self) -> RGB:
        """The HSV state as 0-255 RGB (may differ by rounding from what was typed; see current())."""
        r, g, b = colorsys.hsv_to_rgb(self.h, self.s, self.v)
        return (round(r * 255), round(g * 255), round(b * 255))

    def set(self, rgb: Sequence[int]) -> None:
        """Show colour ``rgb`` (0-255) exactly."""
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        r, g, b = (int(c) for c in rgb)
        self._exact = (r, g, b)
        self._update(None)

    def _pick(self, rgb: RGB) -> None:
        """A swatch was clicked."""
        self.set(rgb)

    def _update(self, source: str | None) -> None:
        """Refresh every field except ``source`` (the one the user is typing in), and redraw.

        Args:
            source: "hex", "rgb", "lin" or None (refresh all).
        """
        rgb = self._exact or self.get()
        self._exact = None
        self._mute = True
        if source != "hex":
            self.hex.set(_hex(rgb))
        if source != "rgb":
            for v, c in zip(self.rgbv, rgb, strict=True):
                v.set(str(c))
        if source != "lin":
            for v, c in zip(self.linv, rgb, strict=True):
                v.set(f"{srgb_to_linear(c / 255):.4f}".rstrip("0").rstrip(".") or "0")
        self._mute = False
        self.hsv_lbl.configure(text=f"{self.h * 360:.0f}°  {self.s * 100:.0f}%  {self.v * 100:.0f}%")
        self._draw(rgb)
        if self.on_change:
            self.on_change(rgb)
        self._cur = rgb

    def current(self) -> RGB:
        """The colour as last shown (exactly as typed, when it was typed)."""
        return getattr(self, "_cur", self.get())

    def _draw(self, rgb: RGB) -> None:
        """Redraw the square (only when the hue changed), both markers and the preview."""
        hr, hg, hb = colorsys.hsv_to_rgb(self.h, 1, 1)
        hue_rgb = (round(hr * 255), round(hg * 255), round(hb * 255))
        if getattr(self, "_drawn_hue", None) != hue_rgb:
            self._drawn_hue = hue_rgb
            horiz = Image.linear_gradient("L").rotate(90).resize((SQ, SQ))
            vert = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize((SQ, SQ))
            base = Image.composite(
                Image.new("RGB", (SQ, SQ), hue_rgb), Image.new("RGB", (SQ, SQ), "white"), horiz
            )
            img = ImageChops.multiply(base, Image.merge("RGB", (vert, vert, vert)))
            self._sq_photo = ImageTk.PhotoImage(img)
            self.sq.itemconfigure(self._sq_img, image=self._sq_photo)
        x, y = self.s * (SQ - 1), (1 - self.v) * (SQ - 1)
        self.sq.coords(self._sq_mark, x - 6, y - 6, x + 6, y + 6)
        self.sq.itemconfigure(
            self._sq_mark, outline="#000000" if self.v > 0.6 and self.s < 0.4 else "#ffffff"
        )
        hy = self.h * (SQ - 1)
        self.hue.coords(self._hue_mark, 1, hy - 2, HUE_W, hy + 2)
        self.prev.delete("sw")
        self.prev.create_rectangle(0, 0, 60, 46, fill=_hex(self.initial), outline="", tags="sw")
        self.prev.create_rectangle(60, 0, 122, 46, fill=_hex(rgb), outline="", tags="sw")

    def _sq_click(self, e: tk.Event) -> None:
        """Click/drag in the square: x = saturation, y = brightness."""
        self.s = min(1.0, max(0.0, e.x / (SQ - 1)))
        self.v = min(1.0, max(0.0, 1 - e.y / (SQ - 1)))
        self._update(None)

    def _hue_click(self, e: tk.Event) -> None:
        """Click/drag in the hue strip (kept below 1.0, which would wrap round to red)."""
        self.h = min(0.9999, max(0.0, e.y / (SQ - 1)))
        self._update(None)

    def _from_hex(self) -> None:
        """Hex field edited (ignored until it is a valid colour)."""
        if self._mute:
            return
        try:
            rgb = _rgb(self.hex.get())
        except ValueError:
            return
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self._exact = rgb
        self._update("hex")

    def _from_rgb(self) -> None:
        """RGB spinboxes edited (values clamped to 0-255)."""
        if self._mute:
            return
        try:
            r, g, b = (max(0, min(255, int(float(v.get() or 0)))) for v in self.rgbv)
        except ValueError:
            return
        rgb = (r, g, b)
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self._exact = rgb
        self._update("rgb")

    def _from_lin(self) -> None:
        """SCS (linear) fields edited, with either decimal separator (values clamped to 0-1)."""
        if self._mute:
            return
        try:
            lin = [max(0.0, min(1.0, float(v.get().replace(",", ".")))) for v in self.linv]
        except ValueError:
            return
        r, g, b = (round(linear_to_srgb(c) * 255) for c in lin)
        rgb = (r, g, b)
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self._exact = rgb
        self._update("lin")


class ColorPicker:
    """Modal colour dialog: a :class:`ColorPanel` with OK and Cancel. Use :meth:`run`."""

    def __init__(self, app: Any, initial: Sequence[int] = (255, 255, 255), title: str = "Select colour"):
        self.app = app
        self.result: RGB | None = None
        w = self.win = tool_window(app, "colorpicker", f"{title} - Paintbooth", resizable=False)
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        self.panel = ColorPanel(f, initial, on_double=self._double, settings=app.settings)
        self.panel.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="Cancel", command=w.destroy).pack(side="right")
        ttk.Button(bar, text="OK", style="Accent.TButton", command=self._ok).pack(side="right", padx=6)
        w.bind("<Return>", lambda e: self._ok())
        w.bind("<Escape>", lambda e: w.destroy())

    def _double(self, rgb: RGB) -> None:
        """Double-clicked swatch: pick it and close."""
        self.panel.set(rgb)
        self._ok()

    def _ok(self) -> None:
        """OK: remember the colour as recent and close."""
        self.result = self.panel.current()
        remember_recent(self.result, self.app.settings)
        self.win.destroy()

    def run(self) -> RGB | None:
        """Show the dialog modally; return the chosen colour, or None if cancelled."""
        self.win.wait_visibility()
        self.win.grab_set()
        self.win.wait_window()
        return self.result


def ask_color(
    initial: Sequence[int] = (255, 255, 255),
    title: str = "Select colour",
    parent: tk.Misc | None = None,
    app: Any = None,
) -> RGB | None:
    """Ask for a colour.

    Args:
        initial: Starting colour (0-255).
        title: Dialog title.
        parent: Parent window for Tk's fallback chooser.
        app: The main window; default: the one registered with :func:`configure`.

    Returns:
        ``(r, g, b)`` 0-255, or None if cancelled. Without any app, Tk's own chooser is used.
    """
    app = app if app is not None else _REGISTRY.app
    if app is None:
        from tkinter import colorchooser

        res = colorchooser.askcolor(color=_hex(initial), parent=parent, title=title)  # type: ignore[arg-type]
        if res and res[0]:
            r, g, b = (int(c) for c in res[0])
            return (r, g, b)
        return None
    return ColorPicker(app, initial, title).run()
