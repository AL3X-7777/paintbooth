"""Colours, fonts and ttk styles for the Paintbooth window (dark theme by default, light on request).

Every widget module reads colours from :data:`C`, the *current* palette, so :func:`apply` swaps the
palette in place (``C.clear(); C.update(...)``) instead of rebinding the name. Modules that did
``from .theme import C`` keep seeing the active colours.

Text size and UI scale (Settings > Appearance) work through Tk's own ``tk scaling`` plus a set of
named fonts (``SkBold``, ``SkHead``, ``SkStart``...). Widgets refer to those fonts by name, so a new
size applies everywhere without touching the widgets. :data:`SCALE` tells :mod:`.icons` how large to
draw icons.

The module also owns the one helper that asks Windows for a dark title bar (:func:`dark_titlebar`);
:mod:`.winutil` re-exports it for the tool windows.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Any

log = logging.getLogger(__name__)

DARK: dict[str, str] = {
    "bg": "#1f2023",
    "panel": "#26272b",
    "raised": "#2e3035",
    "field": "#18191c",
    "fg": "#e3e4e8",
    "muted": "#9a9ea6",
    "border": "#3a3d43",
    "accent": "#2f74d0",
    "accent_fg": "#ffffff",
    "hover": "#353840",
    "select": "#264f84",
    "ok": "#5cbf73",
    "warn": "#e6a23c",
    "err": "#ef5b55",
    "ribbon": "#2a2b30",
    "tab_active": "#1f2023",
    "code_tag": "#e8875b",
    "code_attr": "#79b0ef",
    "code_str": "#8fd18f",
    "code_comment": "#6f747c",
    "errline": "#4a2526",
    "curline": "#202227",
    "match": "#5a4a12",
    "match_cur": "#c8901a",
}
LIGHT: dict[str, str] = {
    "bg": "#f3f3f3",
    "panel": "#ffffff",
    "raised": "#fbfbfb",
    "field": "#ffffff",
    "fg": "#1c1d20",
    "muted": "#61656c",
    "border": "#cfd2d6",
    "accent": "#1b67c9",
    "accent_fg": "#ffffff",
    "hover": "#e8eef7",
    "select": "#cfe0f7",
    "ok": "#2f8a45",
    "warn": "#a96a07",
    "err": "#c62a24",
    "ribbon": "#ffffff",
    "tab_active": "#f3f3f3",
    "code_tag": "#b6461c",
    "code_attr": "#1f5fa8",
    "code_str": "#227a3a",
    "code_comment": "#6a737d",
    "errline": "#fde2e2",
    "curline": "#f4f7fb",
    "match": "#fff1a8",
    "match_cur": "#ffc94d",
}

#: The active palette. Updated in place by :func:`apply`; never rebind it.
C: dict[str, str] = dict(DARK)

#: Named fonts created here. Tk deletes a named font when its Python object is garbage-collected,
#: so the objects are kept alive for the life of the process.
_FONTS: dict[str, tkfont.Font] = {}

#: ``ui``: the user's UI scale (1.0 = 100 %); ``icons``: the factor :mod:`.icons` multiplies icon
#: sizes by; ``font``: the base text size in points.
SCALE: dict[str, float] = {"ui": 1.0, "icons": 1.0, "font": 9}

#: Tk's ``tk scaling`` as it was before the first :func:`apply` (Windows' display scaling). Kept so
#: applying a new UI scale multiplies the original value rather than the previous result.
_BASE_SCALING: list[float] = []

# Font families tried in order; the first one installed wins.
_UI_FAMILIES = ("Segoe UI", "Inter", "Cantarell", "DejaVu Sans")
_MONO_FAMILIES = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono")

# DwmSetWindowAttribute ids for DWMWA_USE_IMMERSIVE_DARK_MODE: 20 on Windows 10 20H1 and later,
# 19 on older Windows 10 builds.
_DWM_DARK_MODE_ATTRS = (20, 19)


def dark_titlebar(win: tk.Misc, dark: bool = True) -> None:
    """Ask Windows 10/11 to draw a window's title bar dark or light.

    Has no effect on other systems or on Windows versions without the attribute.

    Args:
        win: The ``Tk`` or ``Toplevel`` window.
        dark: True for a dark title bar, False for the normal light one.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        win.update_idletasks()
        # Tk's winfo_id is the client area; its parent is the frame window that owns the title bar.
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        val = ctypes.c_int(1 if dark else 0)
        for attr in _DWM_DARK_MODE_ATTRS:
            set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
            if set_attr(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except (OSError, AttributeError, tk.TclError):
        log.debug("dark title bar not available", exc_info=True)


def apply(root: tk.Tk, mode: str = "dark", font_size: int = 9, ui_scale: float = 1.0) -> dict[str, str]:
    """Switch the whole application to a theme, text size and UI scale.

    Safe to call again at run time (Settings does this when the text size changes). Widgets that
    use named styles and fonts update by themselves; classic Tk widgets only pick up the option
    database for widgets created afterwards.

    Args:
        root: The application's ``Tk`` root.
        mode: ``"dark"`` or ``"light"``.
        font_size: Base text size in points (9 is the default); clamped to 7-20.
        ui_scale: 1.0 = 100 %; scales text and icons on top of Windows' own display scaling.
            Clamped to 0.7-2.5.

    Returns:
        The active palette (:data:`C`).
    """
    C.clear()
    C.update(DARK if mode == "dark" else LIGHT)
    _apply_scaling(root, ui_scale)
    d = _apply_fonts(root, font_size)
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    link = C["accent"] if mode == "light" else "#6aa6ff"  # the dark accent is too dim for link text

    s = ttk.Style(root)
    s.theme_use("clam")
    _style_base(s, link)
    _style_buttons(s)
    _style_fields(s)
    _style_lists(s, line)
    _create_fonts(root, _START_FONTS, d)
    _style_start_screen(s)
    _style_sidebar(s)
    _style_chrome(s)
    _apply_option_db(root)
    dark_titlebar(root, mode == "dark")
    return C


# ----------------------------------------------------------------------------- scaling and fonts


def _apply_scaling(root: tk.Tk, ui_scale: float) -> None:
    """Set ``tk scaling`` from the user's UI scale and record the factors in :data:`SCALE`."""
    if not _BASE_SCALING:
        _BASE_SCALING.append(float(root.tk.call("tk", "scaling")))
    base_scaling = _BASE_SCALING[0]
    ui_scale = max(0.7, min(2.5, float(ui_scale or 1.0)))
    root.tk.call("tk", "scaling", base_scaling * ui_scale)
    SCALE["ui"] = ui_scale
    # 1.3333 pixels per point is Tk's value at 96 dpi; above that Windows is scaling the display,
    # and icons follow it too.
    SCALE["icons"] = ui_scale * max(1.0, base_scaling / 1.3333)


# Extra named fonts: (name, size offset from 9 pt or None to keep the base size, bold).
_MAIN_FONTS: tuple[tuple[str, int | None, bool], ...] = (
    ("SkBold", None, True),
    ("SkSection", 10, True),
    ("SkHead", 13, True),
    ("SkSmall", 8, False),
)
# Bigger text on the start screen and in the File side bar.
_START_FONTS: tuple[tuple[str, int | None, bool], ...] = (
    ("SkSide", 12, False),
    ("SkSideSmall", 11, False),
    ("SkSideHead", 10, True),
    ("SkStartTitle", 26, True),
    ("SkStartHead", 13, True),
    ("SkStart", 11, False),
)


def _apply_fonts(root: tk.Tk, font_size: int) -> int:
    """Configure Tk's standard fonts and the main named fonts.

    Returns:
        The offset (in points) of the chosen size from the default 9 pt, which every other named
        size is shifted by.
    """
    fs = max(7, min(20, int(font_size or 9)))
    SCALE["font"] = fs
    d = fs - 9
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        f = tkfont.nametofont(name)
        fams = set(tkfont.families(root))
        for fam in _UI_FAMILIES:
            if fam in fams:
                f.configure(family=fam)
                break
        f.configure(size=fs)
    _create_fonts(root, _MAIN_FONTS, d)
    fixed = tkfont.nametofont("TkFixedFont")
    for fam in _MONO_FAMILIES:
        if fam in set(tkfont.families(root)):
            fixed.configure(family=fam)
            break
    fixed.configure(size=10 + d)
    return d


def _create_fonts(root: tk.Tk, specs: tuple[tuple[str, int | None, bool], ...], d: int) -> None:
    """Create (once) and configure named fonts derived from ``TkDefaultFont``."""
    base = tkfont.nametofont("TkDefaultFont")
    for name, size, bold in specs:
        kw: dict[str, Any] = {}
        if size is not None:
            kw["size"] = size + d
        if bold:
            kw["weight"] = "bold"
        if name not in _FONTS or name not in tkfont.names(root):  # new font, or a new Tk root
            _FONTS[name] = tkfont.Font(root, name=name, exists=False)
        _FONTS[name].configure(**{**base.actual(), **kw})


# ----------------------------------------------------------------------------- ttk styles


def _flat(color: str) -> dict[str, str]:
    """Background, border and both 3D edge colours set to one colour (a flat 'clam' element)."""
    return {"background": color, "bordercolor": color, "lightcolor": color, "darkcolor": color}


def _style_base(s: ttk.Style, link: str) -> None:
    """Defaults for every widget, frames and labels, and the notice/banner label variants."""
    s.configure(
        ".",
        background=C["bg"],
        foreground=C["fg"],
        fieldbackground=C["field"],
        bordercolor=C["border"],
        darkcolor=C["bg"],
        lightcolor=C["bg"],
        troughcolor=C["panel"],
        focuscolor=C["accent"],
        selectbackground=C["select"],
        selectforeground=C["fg"],
        insertcolor=C["fg"],
        arrowcolor=C["fg"],
        font="TkDefaultFont",
    )
    s.map(".", foreground=[("disabled", C["muted"])], background=[("disabled", C["bg"])])
    for st in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton", "TLabelframe", "TLabelframe.Label"):
        s.configure(st, background=C["bg"], foreground=C["fg"])
    s.configure("Panel.TFrame", background=C["panel"])
    s.configure("Panel.TLabel", background=C["panel"])
    s.configure("Ribbon.TFrame", background=C["ribbon"])
    s.configure("Ribbon.TLabel", background=C["ribbon"], foreground=C["muted"], font="SkSmall")
    s.configure("Muted.TLabel", foreground=C["muted"])
    s.configure("Head.TLabel", font="SkHead")
    s.configure("Section.TLabel", font="SkSection", foreground=C["fg"])
    s.configure("Ok.TLabel", foreground=C["ok"])
    s.configure("Warn.TLabel", foreground=C["warn"])
    s.configure("Err.TLabel", foreground=C["err"])
    s.configure("Link.TLabel", foreground=link)
    s.configure("NoticeHead.TLabel", background=C["panel"], font="SkSection", foreground=C["ok"])
    s.configure("Notice.TLabel", background=C["panel"])
    s.configure("NoticeLink.TLabel", background=C["panel"], foreground=link, font="SkBold")
    s.configure("NoticeHint.TLabel", background=C["panel"], foreground=C["muted"], font="SkSmall")
    s.configure("TCheckbutton", indicatorbackground=C["field"], indicatorforeground=C["fg"])
    s.map(
        "TCheckbutton",
        background=[("active", C["bg"])],
        indicatorbackground=[("selected", C["accent"]), ("disabled", C["bg"])],
        indicatorforeground=[("selected", C["accent_fg"])],
    )


def _style_buttons(s: ttk.Style) -> None:
    """Plain, accent, tool, ribbon and File buttons."""
    s.configure(
        "TButton",
        background=C["raised"],
        foreground=C["fg"],
        bordercolor=C["border"],
        padding=(10, 3),
        lightcolor=C["raised"],
        darkcolor=C["raised"],
        focusthickness=0,
    )
    s.map(
        "TButton",
        background=[("pressed", C["select"]), ("active", C["hover"]), ("disabled", C["bg"])],
        bordercolor=[("active", C["accent"])],
    )
    s.configure("Accent.TButton", foreground=C["accent_fg"], **_flat(C["accent"]))
    s.map("Accent.TButton", background=[("active", C["select"]), ("disabled", C["raised"])])
    s.configure("Wide.TButton", padding=(10, 4), anchor="center")
    s.configure("Tool.TButton", padding=(3, 1), **_flat(C["bg"]))
    s.map("Tool.TButton", background=[("active", C["hover"])], bordercolor=[("active", C["border"])])
    s.configure(
        "Ribbon.TButton",
        padding=(6, 4),
        width=-7,  # negative = minimum width in characters, so short labels still line up
        font="SkSmall",
        justify="center",
        **_flat(C["ribbon"]),
    )
    s.map(
        "Ribbon.TButton",
        background=[("active", C["hover"]), ("pressed", C["select"])],
        bordercolor=[("active", C["border"])],
        foreground=[("disabled", C["muted"])],
    )
    s.configure("RibbonTab.TButton", padding=(10, 3), **_flat(C["bg"]))
    s.map("RibbonTab.TButton", background=[("active", C["hover"])])
    s.configure("RibbonTabOn.TButton", padding=(10, 3), **{**_flat(C["ribbon"]), "bordercolor": C["border"]})
    s.configure("File.TButton", padding=(14, 3), foreground=C["accent_fg"], **_flat(C["accent"]))
    s.map("File.TButton", background=[("active", C["select"])])


def _style_fields(s: ttk.Style) -> None:
    """Entries, spin boxes and combo boxes."""
    field_edges = {"lightcolor": C["field"], "darkcolor": C["field"]}
    s.configure(
        "TEntry",
        fieldbackground=C["field"],
        foreground=C["fg"],
        bordercolor=C["border"],
        padding=3,
        **field_edges,
    )
    s.map("TEntry", bordercolor=[("focus", C["accent"])], fieldbackground=[("disabled", C["bg"])])
    s.configure(
        "TSpinbox",
        fieldbackground=C["field"],
        foreground=C["fg"],
        bordercolor=C["border"],
        arrowsize=10,
        padding=2,
        background=C["raised"],
        **field_edges,
    )
    s.map("TSpinbox", bordercolor=[("focus", C["accent"])], fieldbackground=[("disabled", C["bg"])])
    s.configure(
        "TCombobox",
        fieldbackground=C["field"],
        foreground=C["fg"],
        background=C["raised"],
        bordercolor=C["border"],
        arrowsize=12,
        **field_edges,
    )
    s.map(
        "TCombobox",
        fieldbackground=[("readonly", C["field"])],
        foreground=[("readonly", C["fg"])],
        selectbackground=[("readonly", C["field"])],
        selectforeground=[("readonly", C["fg"])],
    )


def _style_lists(s: ttk.Style, line: int) -> None:
    """Notebooks and tree views; row heights follow the font's line height."""
    s.configure("TNotebook", background=C["bg"], bordercolor=C["border"], tabmargins=(4, 4, 0, 0))
    s.configure(
        "TNotebook.Tab",
        foreground=C["muted"],
        padding=(10, 3),
        **{**_flat(C["panel"]), "bordercolor": C["border"]},
    )
    s.map(
        "TNotebook.Tab",
        background=[("selected", C["tab_active"]), ("active", C["hover"])],
        foreground=[("selected", C["fg"])],
        lightcolor=[("selected", C["tab_active"])],
    )
    s.configure(
        "Treeview",
        fieldbackground=C["field"],
        foreground=C["fg"],
        rowheight=line + 7,
        **{**_flat(C["field"]), "bordercolor": C["border"]},
    )
    s.map("Treeview", background=[("selected", C["accent"])], foreground=[("selected", C["accent_fg"])])
    s.configure(
        "Treeview.Heading",
        foreground=C["muted"],
        **{**_flat(C["panel"]), "bordercolor": C["border"]},
    )
    s.map("Treeview.Heading", background=[("active", C["hover"])])
    s.configure("Items.Treeview", rowheight=line + 6, background=C["panel"], fieldbackground=C["panel"])


def _style_start_screen(s: ttk.Style) -> None:
    """The start screen: big title, headings, buttons and the recent-projects list."""
    s.configure("Start.TFrame", background=C["bg"])
    s.configure("StartTitle.TLabel", font="SkStartTitle", background=C["bg"])
    s.configure("StartHead.TLabel", font="SkStartHead", background=C["bg"])
    s.configure("StartMuted.TLabel", font="SkStart", foreground=C["muted"], background=C["bg"])
    s.configure("Start.TButton", font="SkStart", padding=(16, 9), anchor="w")
    s.configure(
        "StartAccent.TButton",
        font="SkStart",
        padding=(16, 9),
        anchor="w",
        background=C["accent"],
        foreground=C["accent_fg"],
        bordercolor=C["accent"],
    )
    s.map("StartAccent.TButton", background=[("active", C["select"])])
    s.configure("Start.Treeview", rowheight=_FONTS["SkStart"].metrics("linespace") + 12, font="SkStart")
    s.configure("Start.Treeview.Heading", font="SkStart")


def _style_sidebar(s: ttk.Style) -> None:
    """The File side bar: panel, large menu buttons, shortcut and heading labels."""
    s.configure("Side.TFrame", background=C["panel"], bordercolor=C["border"], relief="solid", borderwidth=1)
    for name, font, padding in (
        ("Side.TButton", "SkSide", (20, 9)),
        ("SideSmall.TButton", "SkSideSmall", (28, 6)),
    ):
        s.configure(name, font=font, padding=padding, anchor="w", **_flat(C["panel"]))
        s.map(name, background=[("active", C["hover"]), ("pressed", C["select"])])
    s.configure("SideKey.TLabel", font="SkSideSmall", foreground=C["muted"], background=C["panel"])
    s.configure("SideHead.TLabel", font="SkSideHead", foreground=C["muted"], background=C["panel"])


def _style_chrome(s: ttk.Style) -> None:
    """Scroll bars, paned-window sashes, separators and the 'file changed' banner."""
    for orient in ("Vertical", "Horizontal"):
        s.configure(
            f"{orient}.TScrollbar",
            background=C["raised"],
            troughcolor=C["bg"],
            bordercolor=C["bg"],
            lightcolor=C["raised"],
            darkcolor=C["raised"],
            arrowcolor=C["muted"],
            gripcount=0,
        )
        s.map(f"{orient}.TScrollbar", background=[("active", C["hover"])])
    s.configure("TPanedwindow", background=C["border"])
    s.configure("Sash", sashthickness=4, gripcount=0, background=C["border"])
    s.configure("TSeparator", background=C["border"])
    s.configure("Banner.TFrame", background=C["select"])
    s.configure("Banner.TLabel", background=C["select"], foreground=C["fg"])


def _apply_option_db(root: tk.Tk) -> None:
    """Colours for classic Tk widgets (menus, Text, Listbox, Canvas), set through the option database."""
    root.configure(background=C["bg"])
    for pattern, value in (
        ("*Menu.background", C["panel"]),
        ("*Menu.foreground", C["fg"]),
        ("*Menu.activeBackground", C["accent"]),
        ("*Menu.activeForeground", C["accent_fg"]),
        ("*Menu.disabledForeground", C["muted"]),
        ("*Menu.relief", "flat"),
        ("*Menu.borderWidth", 1),
        ("*TCombobox*Listbox.background", C["field"]),
        ("*TCombobox*Listbox.foreground", C["fg"]),
        ("*TCombobox*Listbox.selectBackground", C["accent"]),
        ("*Text.background", C["field"]),
        ("*Text.foreground", C["fg"]),
        ("*Text.insertBackground", C["fg"]),
        ("*Text.selectBackground", C["select"]),
        ("*Canvas.background", C["bg"]),
        ("*Listbox.background", C["field"]),
        ("*Listbox.foreground", C["fg"]),
        ("*Toplevel.background", C["bg"]),
    ):
        root.option_add(pattern, value)
