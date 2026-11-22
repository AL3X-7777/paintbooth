"""The Office-style ribbon at the top of the main window, which the user can rearrange.

The ribbon has a File button (opens :class:`~.startscreen.FileSidebar`) and one tab, Home, whose
page holds captioned groups of large buttons. Groups flow left to right and wrap onto a new row
when the window is too narrow for them (large text or UI scale).

Which buttons sit in which group, and in what order, is a *layout*: a list of
``{"caption": ..., "items": [button id, ...]}`` kept in the settings (``ribbon_layout``). Every
button has an id in :data:`BUTTONS` (or ``add:<kind>`` for an "Add more" entry). The user changes
the layout by dragging:

* a button onto another button (it goes before that one) or onto a group's caption (it goes at
  the end of that group);
* an entry of the "Add more" list (which opens when the pointer rests on the button) onto the
  ribbon, which adds it as a button of its own.

Right-clicking a button or a caption offers the same moves, plus new group, rename, remove and
*Reset ribbon*. The ribbon only calls methods of the :class:`~.main.App`; apart from the layout it
holds no state of its own except which tab is shown and whether the ribbon is collapsed.
"""

from __future__ import annotations

import contextlib
import copy
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from tkinter import simpledialog, ttk
from typing import TYPE_CHECKING, Any

from .. import ms2data
from .widgets import Tooltip

if TYPE_CHECKING:
    from .main import App


@dataclass(frozen=True)
class Button:
    """One kind of ribbon button: label, icon name, what it does (given the app), tooltip."""

    label: str
    icon: str
    action: Callable[[App], object]
    tip: str


#: Every button the ribbon can show, by id.
BUTTONS: dict[str, Button] = {
    "new": Button("New", "new", lambda a: a.new_dialog(), "New project (Ctrl+N)"),
    "open": Button(
        "Open", "open", lambda a: a.open_dialog(), "Open a project, .pb or Mods Studio 2 .ms2 (Ctrl+O)"
    ),
    "save": Button("Save", "save", lambda a: a.save(), "Save (Ctrl+S)"),
    "saveas": Button("Save as", "saveas", lambda a: a.save_as(), "Save under a new name (Ctrl+Shift+S)"),
    "export_mod_folder": Button(
        "Export to\nmod folder",
        "export",
        lambda a: a.export(to_mod_folder=True),
        "Build the mod straight into the game's mod folder (Shift+F5)",
    ),
    "manifest": Button(
        "Mod\nmanifest", "tobj", lambda a: a.show_manifest(), "Mod name, author, icon, description (Ctrl+M)"
    ),
    "add_skin": Button("Add\nSkin", "add", lambda a: a.add_item(), "Add an Advanced Skin (truck or trailer)"),
    "add_more": Button(
        "Add\nmore",
        "add",
        lambda a: a.show_add_menu(),
        "Lightbox, windscreen decal, door window, billboard, cargo container, company logo, existing "
        "mod. Rest the pointer here to open the list; drag an entry onto the ribbon to give it a "
        "button of its own.",
    ),
    "duplicate": Button(
        "Duplicate", "duplicate", lambda a: a.duplicate_item(), "Duplicate the selected item (Ctrl+D)"
    ),
    "delete": Button("Delete", "delete", lambda a: a.delete_item(), "Delete the selected item (Del)"),
    "export": Button("Export", "export", lambda a: a.export(), "Build the mod as .scs (F5)"),
    "to_mod_folder": Button(
        "To mod\nfolder",
        "export",
        lambda a: a.export(to_mod_folder=True),
        "Build straight into the game's mod folder (Shift+F5)",
    ),
    "show_folder": Button(
        "Show\nfolder", "folder", lambda a: a.show_export(), "Show the exported mod in Explorer"
    ),
    "color": Button(
        "Color\nconverter", "color", lambda a: a.open_color_converter(), "sRGB <-> SCS linear colour values"
    ),
    "tobj": Button(
        "TOBJ\nEditor", "tobj", lambda a: a.open_tobj_editor(), "View and edit .tobj texture objects"
    ),
    "vehicles": Button(
        "Vehicles", "vehicles", lambda a: a.open_vehicle_manager(), "Vehicle library: edit XML, scan the game"
    ),
    "settings": Button(
        "Settings",
        "gear",
        lambda a: a.open_settings(),
        "Export folders, creator name, versions, default icon…",
    ),
    "tutorials": Button(
        "Tutorials",
        "help",
        lambda a: a.open_tutorials(),
        "How to use Paintbooth and make paint job mods (F1)",
    ),
    "github": Button(
        "GitHub", "open", lambda a: a.open_github(), "Paintbooth on GitHub: downloads, news, source code"
    ),
}

#: The layout a new user gets (and *Reset ribbon* brings back).
DEFAULT_LAYOUT: list[dict[str, Any]] = [
    {"caption": "Project", "items": ["new", "open", "save", "saveas", "export_mod_folder"]},
    {"caption": "Items", "items": ["manifest", "add_skin", "add_more", "duplicate", "delete"]},
    {"caption": "Export", "items": ["export", "to_mod_folder", "show_folder"]},
    {"caption": "Converters", "items": ["color"]},
    {"caption": "File utilities", "items": ["tobj"]},
    {"caption": "Resources", "items": ["vehicles"]},
    {"caption": "Program", "items": ["settings", "tutorials", "github"]},
]

#: How far (pixels) the pointer must move with the button held before a click becomes a drag.
DRAG_THRESHOLD = 6
#: How long (ms) the pointer rests on "Add more" before its list opens.
HOVER_MS = 350


def button_for(item_id: str) -> Button | None:
    """The :class:`Button` of a layout id, including ``add:<kind>`` entries of "Add more"."""
    if item_id in BUTTONS:
        return BUTTONS[item_id]
    if item_id.startswith("add:"):
        kind = item_id[4:]
        label, help_ = ms2data.FEATURES.get(kind, (kind, ""))
        words = label.split(" ", 1)
        return Button(
            "\n".join(words), "add", partial(_add_template, kind), f"Add a {label.lower()}: {help_}"
        )
    return None


def clean_layout(layout: object) -> list[dict[str, Any]]:
    """A copy of ``layout`` that is safe to show: known ids only, each once, captions as text.

    Anything unreadable (an old or hand-edited setting) gives the default layout.
    """
    if not isinstance(layout, list) or not layout:
        return copy.deepcopy(DEFAULT_LAYOUT)
    out, seen = [], set()
    for g in layout:
        if not isinstance(g, dict) or not isinstance(g.get("items"), list):
            continue
        items = []
        for i in g["items"]:
            if isinstance(i, str) and button_for(i) is not None and i not in seen:
                seen.add(i)
                items.append(i)
        out.append({"caption": str(g.get("caption", "")), "items": items})
    return out or copy.deepcopy(DEFAULT_LAYOUT)


def move_item(layout: list[dict[str, Any]], item_id: str, group: int, index: int | None = None) -> None:
    """Move (or add) ``item_id`` into ``layout[group]`` at ``index`` (None = the end), in place."""
    target = layout[group]["items"]
    for g in layout:
        if item_id in g["items"]:
            pos = g["items"].index(item_id)
            g["items"].remove(item_id)
            if g is layout[group] and index is not None and pos < index:
                index -= 1  # the list got shorter in front of the target
    target.insert(len(target) if index is None else max(0, min(index, len(target))), item_id)


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
        self.buttons: dict[str, ttk.Button] = {}  # layout id -> its button
        self.captions: list[ttk.Label] = []  # one per group, in layout order
        self.layout = clean_layout(app.settings.get("ribbon_layout"))
        self.pages["Home"] = self._home()
        self.current: str | None = None
        self.collapsed = False
        self._drag: dict[str, Any] | None = None
        self._ghost: tk.Toplevel | None = None
        self._hover_job: str | None = None
        self.add_popup: tk.Toplevel | None = None
        self.show("Home")

    # ---------------------------------------------------------------- building
    def _group(self, parent: _FlowPage, gi: int, caption: str, items: list[str]) -> None:
        """Add one captioned group of large buttons to a page."""
        g = ttk.Frame(parent, style="Ribbon.TFrame")
        row = ttk.Frame(g, style="Ribbon.TFrame")
        row.pack()
        for item_id in items:
            spec = button_for(item_id)
            if spec is None:
                continue
            b = ttk.Button(
                row,
                text=spec.label,
                image=self.app.icons.get(spec.icon, 32),
                compound="top",
                style="Ribbon.TButton",
                command=partial(spec.action, self.app),
            )
            b.pack(side="left", padx=1)
            Tooltip(b, spec.tip)
            self.buttons[item_id] = b
            self._draggable(b, item_id)
            b.bind("<Button-3>", partial(self._button_menu, item_id), add="+")
        if not items:  # an empty group still needs somewhere to drop buttons
            ttk.Label(row, text="drop\nbuttons here", style="Ribbon.TLabel", justify="center").pack(
                padx=12, pady=8
            )
        cap = ttk.Label(g, text=caption, style="Ribbon.TLabel")
        cap.pack(pady=(0, 2))
        cap.bind("<Button-3>", partial(self._group_menu, gi), add="+")
        self.captions.append(cap)
        g._pb_group = gi  # type: ignore[attr-defined]  # found again under the pointer when dropping
        sep = ttk.Separator(parent, orient="vertical")
        parent.flow.append((g, sep))

    def _home(self) -> _FlowPage:
        """Build the Home page from the layout."""
        p = _FlowPage(self.body)
        self.buttons, self.captions = {}, []
        for gi, g in enumerate(self.layout):
            self._group(p, gi, g["caption"], g["items"])
        self.add_more_btn = self.buttons.get("add_more")
        if self.add_more_btn is not None:
            self.add_more_btn.bind("<Enter>", self._hover_start, add="+")
            self.add_more_btn.bind("<Leave>", self._hover_cancel, add="+")
        self._reflow(p)
        return p

    def rebuild(self, save: bool = True) -> None:
        """Rebuild the page after a layout change (and remember the layout)."""
        if save:
            self.app.settings["ribbon_layout"] = copy.deepcopy(self.layout)
        old = self.pages.get("Home")
        self.pages["Home"] = self._home()
        if old is not None:
            old.destroy()
        if self.current == "Home" and not self.collapsed:
            self.pages["Home"].pack(fill="x")

    def reset(self) -> None:
        """Put every button back where it was when Paintbooth was installed."""
        self.layout = copy.deepcopy(DEFAULT_LAYOUT)
        self.app.settings.pop("ribbon_layout", None)
        self.rebuild(save=False)

    # ---------------------------------------------------------------- dragging
    def _draggable(self, w: tk.Widget, item_id: str, on_click: Callable[[], object] | None = None) -> None:
        """Let ``w`` be dragged onto the ribbon; a press without moving stays a click."""
        w.bind("<ButtonPress-1>", lambda e: self._press(e, item_id, on_click), add="+")
        w.bind("<B1-Motion>", self._motion, add="+")
        w.bind("<ButtonRelease-1>", self._release, add="+")

    def _press(self, e: tk.Event, item_id: str, on_click: Callable[[], object] | None) -> None:
        self._drag = {"id": item_id, "x": e.x_root, "y": e.y_root, "moving": False, "click": on_click}

    def _motion(self, e: tk.Event) -> None:
        d = self._drag
        if d is None:
            return
        if not d["moving"] and abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) < DRAG_THRESHOLD:
            return
        if not d["moving"]:
            d["moving"] = True
            spec = button_for(d["id"])
            self._ghost = tk.Toplevel(self)
            self._ghost.overrideredirect(True)
            self._ghost.attributes("-topmost", True)
            ttk.Label(
                self._ghost, text=(spec.label if spec else d["id"]).replace("\n", " "), padding=6
            ).pack()
        if self._ghost is not None:
            self._ghost.geometry(f"+{e.x_root + 12}+{e.y_root + 8}")

    def _release(self, e: tk.Event) -> str | None:
        d, self._drag = self._drag, None
        if isinstance(e.widget, ttk.Button):
            e.widget.state(["!pressed"])  # the release is kept from the button after a drag
        if self._ghost is not None:
            self._ghost.destroy()
            self._ghost = None
        if d is None:
            return None
        if not d["moving"]:
            if d["click"] is not None:  # a list entry (not a ttk.Button) was clicked
                d["click"]()
            return None
        target = self.drop_target(e.x_root, e.y_root)
        if target is not None:
            self.drop(d["id"], *target)
        return "break"  # a dragged ttk.Button must not also run its command

    def drop_target(self, x: int, y: int) -> tuple[int, int | None] | None:
        """Where a button dropped at screen point (x, y) goes: (group, index or None = end)."""
        w: Any = self.winfo_containing(x, y)
        while w is not None and w is not self:
            for item_id, b in self.buttons.items():
                if b is w:
                    for gi, g in enumerate(self.layout):
                        if item_id in g["items"]:
                            after = x > b.winfo_rootx() + b.winfo_width() / 2
                            return gi, g["items"].index(item_id) + (1 if after else 0)
            group = getattr(w, "_pb_group", None)
            if group is not None:
                return group, None
            w = w.master
        return None

    def drop(self, item_id: str, group: int, index: int | None) -> None:
        """Put ``item_id`` at ``index`` of group ``group`` (from a drag) and show the new layout."""
        if not 0 <= group < len(self.layout):
            return
        move_item(self.layout, item_id, group, index)
        self.rebuild()
        if self.add_popup is not None:
            self.close_add_popup()

    # ---------------------------------------------------------------- right-click menus
    def _button_menu(self, item_id: str, e: tk.Event) -> None:
        m = tk.Menu(self, tearoff=False)
        gi = next(i for i, g in enumerate(self.layout) if item_id in g["items"])
        pos = self.layout[gi]["items"].index(item_id)
        m.add_command(label="Move left", command=lambda: self.drop(item_id, gi, max(pos - 1, 0)))
        m.add_command(label="Move right", command=lambda: self.drop(item_id, gi, pos + 2))
        to = tk.Menu(m, tearoff=False)
        for gj, g in enumerate(self.layout):
            if gj != gi:
                to.add_command(
                    label=g["caption"] or f"Group {gj + 1}", command=partial(self.drop, item_id, gj, None)
                )
        to.add_separator()
        to.add_command(label="New group…", command=lambda: self._new_group(item_id))
        m.add_cascade(label="Move to group", menu=to)
        m.add_command(label="Remove from the ribbon", command=lambda: self._remove(item_id))
        m.add_separator()
        m.add_command(label="Reset ribbon", command=self.reset)
        m.tk_popup(e.x_root, e.y_root)

    def _group_menu(self, gi: int, e: tk.Event) -> None:
        m = tk.Menu(self, tearoff=False)
        m.add_command(
            label="Move group left",
            command=lambda: self._move_group(gi, -1),
            state="normal" if gi else "disabled",
        )
        m.add_command(
            label="Move group right",
            command=lambda: self._move_group(gi, 1),
            state="normal" if gi < len(self.layout) - 1 else "disabled",
        )
        m.add_command(label="Rename group…", command=lambda: self._rename(gi))
        m.add_command(label="New group…", command=lambda: self._new_group(None, gi + 1))
        m.add_command(
            label="Remove group",
            command=lambda: self._remove_group(gi),
            state="normal" if not self.layout[gi]["items"] else "disabled",
        )
        m.add_separator()
        m.add_command(label="Reset ribbon", command=self.reset)
        m.tk_popup(e.x_root, e.y_root)

    def _remove(self, item_id: str) -> None:
        for g in self.layout:
            if item_id in g["items"]:
                g["items"].remove(item_id)
        self.rebuild()
        self.app.status("Removed from the ribbon - Reset ribbon (right-click) brings every button back")

    def _move_group(self, gi: int, step: int) -> None:
        gj = gi + step
        self.layout[gi], self.layout[gj] = self.layout[gj], self.layout[gi]
        self.rebuild()

    def _rename(self, gi: int) -> None:
        name = simpledialog.askstring(
            "Rename group", "Group name:", initialvalue=self.layout[gi]["caption"], parent=self
        )
        if name is not None:
            self.layout[gi]["caption"] = name.strip()
            self.rebuild()

    def _new_group(self, item_id: str | None, at: int | None = None) -> None:
        name = simpledialog.askstring("New group", "Name of the new group:", parent=self)
        if not name:
            return
        pos = len(self.layout) if at is None else at
        self.layout.insert(pos, {"caption": name.strip(), "items": []})
        if item_id is not None:
            move_item(self.layout, item_id, pos, None)
        self.rebuild()

    def _remove_group(self, gi: int) -> None:
        if not self.layout[gi]["items"]:
            del self.layout[gi]
            self.rebuild()

    # ---------------------------------------------------------------- "Add more" list
    def _hover_start(self, _e: tk.Event) -> None:
        self._hover_cancel()
        self._hover_job = self.after(HOVER_MS, self.app.show_add_menu)

    def _hover_cancel(self, _e: object = None) -> None:
        if self._hover_job is not None:
            self.after_cancel(self._hover_job)
            self._hover_job = None

    def open_add_popup(self, entries: list[tuple[str, str, Callable[[], object] | None]]) -> None:
        """Show the "Add more" list under its button (or at the pointer).

        Args:
            entries: ``(label, layout id, action)``; an empty label is a separator, a None
                action a greyed-out line. Entries with a layout id can be dragged onto the ribbon.
        """
        self.close_add_popup()
        self.update_idletasks()  # so a just-rebuilt "Add more" button reports where it is
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        frame = ttk.Frame(top, padding=4, style="Panel.TFrame", relief="solid", borderwidth=1)
        frame.pack()
        for label, item_id, action in entries:
            if not label:
                ttk.Separator(frame).pack(fill="x", pady=3)
                continue
            lab = ttk.Label(
                frame, text=label, padding=(10, 4), style="Menu.TLabel" if action else "MenuOff.TLabel"
            )
            lab.pack(fill="x")
            if action is None:
                continue

            def run(a: Callable[[], object] = action) -> None:
                self.close_add_popup()
                a()

            lab.configure(cursor="hand2")
            lab.bind("<Enter>", partial(_restyle, lab, "MenuHover.TLabel"))
            lab.bind("<Leave>", partial(_restyle, lab, "Menu.TLabel"))
            if item_id:
                self._draggable(lab, item_id, on_click=run)
                Tooltip(
                    lab, "Click to add it to the project, or drag it onto the ribbon for a button of its own."
                )
            else:
                lab.bind("<ButtonRelease-1>", partial(_call, run))
        b = self.add_more_btn
        if b is not None and b.winfo_ismapped():
            top.geometry(f"+{b.winfo_rootx()}+{b.winfo_rooty() + b.winfo_height()}")
        else:
            top.geometry(f"+{self.winfo_pointerx()}+{self.winfo_pointery()}")
        self.add_popup = top
        self.after(300, self._watch_popup)

    def _watch_popup(self) -> None:
        """Close the list once the pointer has left both it and the button (unless dragging)."""
        top = self.add_popup
        if top is None or not top.winfo_exists():
            return
        if self._drag is None:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
            inside = [top] + ([self.add_more_btn] if self.add_more_btn is not None else [])
            if not any(_contains(w, x, y, 12) for w in inside):
                self.close_add_popup()
                return
        self.after(250, self._watch_popup)

    def close_add_popup(self) -> None:
        """Close the "Add more" list, if open."""
        if self.add_popup is not None:
            with contextlib.suppress(tk.TclError):
                self.add_popup.destroy()
            self.add_popup = None

    # ---------------------------------------------------------------- layout and tabs
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


def _add_template(kind: str, app: App) -> None:
    """Action of an ``add:<kind>`` button."""
    app.add_template(kind)


def _restyle(w: ttk.Label, style: str, _e: object = None) -> None:
    w.configure(style=style)


def _call(fn: Callable[[], object], _e: object = None) -> None:
    fn()


def _contains(w: tk.Misc, x: int, y: int, margin: int = 0) -> bool:
    """True if screen point (x, y) lies on widget ``w`` (grown by ``margin`` pixels)."""
    try:
        if not w.winfo_ismapped():
            return False
        x0, y0 = w.winfo_rootx() - margin, w.winfo_rooty() - margin
        return x0 <= x <= x0 + w.winfo_width() + 2 * margin and y0 <= y <= y0 + w.winfo_height() + 2 * margin
    except tk.TclError:
        return False
