"""Start screen (shown when Paintbooth opens) and the File side bar.

Both are frames placed over the main window: :class:`StartScreen` covers the editor area while no
project is open, and :class:`FileSidebar` slides over the left edge when the ribbon's File button
is pressed (like Office's backstage view). The recent-projects list lives in the settings
(``recent`` paths, newest first, and ``recent_times``); :func:`remember` and :func:`forget` edit it.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .main import App

from .. import __version__, appdirs
from .theme import SCALE, C
from .widgets import open_path

MAX_RECENT = 30  # entries kept in settings['recent']


LOGO = Path(__file__).resolve().parent.parent / "data" / "logo.png"
LOGO_PX = 48  # the logo's side on the start screen at 100 % UI scale


def _logo_image(master: tk.Misc) -> Any:
    """Return the Paintbooth logo as a Tk image sized for the start screen, or None if it can't load.

    The caller must keep a reference to the result: Tk drops images that Python has collected.
    """
    try:
        from PIL import Image, ImageTk

        side = max(16, round(LOGO_PX * SCALE["icons"]))
        with Image.open(LOGO) as im:
            return ImageTk.PhotoImage(im.resize((side, side), Image.Resampling.LANCZOS), master=master)
    except (OSError, ValueError, tk.TclError):
        return None


def remember(app: Any, path: Path) -> None:
    """Put a project at the top of the recent list (most recently opened first)."""
    p = str(path)
    s = app.settings
    s["recent"] = ([p] + [x for x in s.get("recent", []) if x != p])[:MAX_RECENT]
    s.setdefault("recent_times", {})[p] = time.time()
    s["recent_times"] = {k: v for k, v in s["recent_times"].items() if k in s["recent"]}
    s["last_project"] = p


def forget(app: Any, path: str) -> None:
    """Remove a project from the recent list (the file itself is not touched)."""
    s = app.settings
    s["recent"] = [x for x in s.get("recent", []) if x != path]
    s.get("recent_times", {}).pop(path, None)


def _when(ts: float | None) -> str:
    """Describe a timestamp the way a person would ("5 min ago", "3 day(s) ago", "06 Nov 2026")."""
    if not ts:
        return ""
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 86400 * 7:
        return f"{int(d // 86400)} day(s) ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


class StartScreen(ttk.Frame):
    """Covers the editor area when no project is open."""

    def __init__(self, app: App) -> None:
        super().__init__(app.root, padding=(48, 36), style="Start.TFrame")
        self.app = app
        head = ttk.Frame(self, style="Start.TFrame")
        head.pack(fill="x")
        self._logo = _logo_image(self)
        if self._logo is not None:
            ttk.Label(head, image=self._logo, style="StartTitle.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(head, text="Paintbooth", style="StartTitle.TLabel").pack(side="left")
        ttk.Label(head, text=f"  {__version__}", style="StartMuted.TLabel").pack(side="left", pady=(14, 0))

        body = ttk.Frame(self, style="Start.TFrame")
        body.pack(fill="both", expand=True, pady=(26, 0))
        left = ttk.Frame(body, style="Start.TFrame")
        left.pack(side="left", fill="y", padx=(0, 48))
        ttk.Label(left, text="Start new project", style="StartHead.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Button(
            left,
            text="New ETS2 project",
            style="StartAccent.TButton",
            command=lambda: app.new_project("ets2"),
        ).pack(fill="x", pady=3)
        ttk.Button(
            left, text="New ATS project", style="StartAccent.TButton", command=lambda: app.new_project("ats")
        ).pack(fill="x", pady=3)
        ttk.Label(left, text="Open", style="StartHead.TLabel").pack(anchor="w", pady=(26, 8))
        ttk.Button(left, text="Open existing project…", style="Start.TButton", command=app.open_dialog).pack(
            fill="x", pady=3
        )
        ttk.Button(
            left,
            text="Open projects folder",
            style="Start.TButton",
            command=lambda: open_path(appdirs.projects_dir()),
        ).pack(fill="x", pady=3)
        ttk.Label(left, text="Learn", style="StartHead.TLabel").pack(anchor="w", pady=(26, 8))
        ttk.Button(left, text="Tutorials", style="Start.TButton", command=app.open_tutorials).pack(
            fill="x", pady=3
        )
        ttk.Button(
            left,
            text="Your first truck skin",
            style="Start.TButton",
            command=lambda: app.open_tutorials("Your first truck skin"),
        ).pack(fill="x", pady=3)

        right = ttk.Frame(body, style="Start.TFrame")
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Recent projects", style="StartHead.TLabel").pack(anchor="w", pady=(0, 8))
        self.tv = ttk.Treeview(
            right,
            columns=("folder", "when"),
            show="tree headings",
            style="Start.Treeview",
            selectmode="browse",
        )
        self.tv.heading("#0", text="Project", anchor="w")
        self.tv.heading("folder", text="Folder", anchor="w")
        self.tv.heading("when", text="Last opened", anchor="w")
        self.tv.column("#0", width=280, stretch=False)
        self.tv.column("folder", width=420)
        self.tv.column("when", width=140, stretch=False)
        self.tv.tag_configure("missing", foreground=C["muted"])
        self.tv.pack(fill="both", expand=True)
        self.tv.bind("<Double-Button-1>", lambda e: self._open())
        self.tv.bind("<Return>", lambda e: self._open())
        self.tv.bind("<Button-3>", self._menu)
        self.tv.bind("<Delete>", lambda e: self._remove())
        self.hint = ttk.Label(
            right,
            text="Double-click to open · right-click to remove from the list",
            style="StartMuted.TLabel",
        )
        self.hint.pack(anchor="w", pady=(6, 0))

    def refresh(self) -> None:
        """Fill the recent-projects list; missing files stay listed, greyed out."""
        self.tv.delete(*self.tv.get_children())
        times = self.app.settings.get("recent_times", {})
        for p in self.app.settings.get("recent", []):
            path = Path(p)
            ok = path.is_file()
            self.tv.insert(
                "",
                "end",
                iid=p,
                text="  " + path.stem + ("" if ok else "   (missing)"),
                values=(str(path.parent), _when(times.get(p))),
                tags=() if ok else ("missing",),
            )
        kids = self.tv.get_children()
        if kids:
            self.tv.selection_set(kids[0])
            self.tv.focus(kids[0])
        self.hint.configure(
            text="Double-click to open · right-click to remove from the list"
            if kids
            else "Projects you open or save will be listed here."
        )

    def show(self) -> None:
        """Cover the editor area and put the keyboard focus on the list."""
        self.refresh()
        self.place(in_=self.app.pane, x=0, y=0, relwidth=1, relheight=1)
        self.lift()
        self.tv.focus_set()

    def hide(self) -> None:
        """Uncover the editor."""
        self.place_forget()

    def _sel(self) -> str | None:
        """Return the selected project path, if any."""
        s = self.tv.selection()
        return s[0] if s else None

    def _open(self) -> None:
        """Open the selected project."""
        p = self._sel()
        if p is None:
            return
        if not Path(p).is_file():
            self.app.status(f"{p} no longer exists")
            return
        self.app.open_project(Path(p))

    def _remove(self) -> None:
        """Remove the selected project from the list."""
        p = self._sel()
        if p:
            forget(self.app, p)
            self.refresh()

    def _menu(self, e: tk.Event) -> None:
        """Right-click menu of a recent project."""
        row = self.tv.identify_row(e.y)
        if not row:
            return
        self.tv.selection_set(row)
        m = tk.Menu(self, tearoff=False)
        m.add_command(label="Open", command=self._open)
        m.add_command(label="Show in folder", command=lambda: open_path(Path(row).parent))
        m.add_separator()
        m.add_command(label="Remove from list", command=self._remove)
        m.tk_popup(e.x_root, e.y_root)


class FileSidebar(ttk.Frame):
    """The File menu as a side bar over the left of the window (bigger text)."""

    WIDTH = 300

    def __init__(self, app: App) -> None:
        super().__init__(app.root, style="Side.TFrame", padding=(0, 8))
        self.app = app
        self.visible = False
        app.root.bind_all("<Button-1>", self._click_anywhere, add="+")
        app.root.bind_all("<Escape>", lambda e: self.hide() if self.visible else None, add="+")

    def _build(self) -> None:
        """Recreate the menu entries (they depend on whether a project is open)."""
        for w in self.winfo_children():
            w.destroy()
        a = self.app

        def item(text: str, cmd: Callable[[], object], key: str = "") -> ttk.Button:
            """One menu entry; ``key`` is the shortcut shown at the right."""
            b = ttk.Button(self, text=f"{text}", style="Side.TButton", command=lambda: self._run(cmd))
            b.pack(fill="x")
            if key:
                ttk.Label(b, text=key, style="SideKey.TLabel").place(relx=1.0, x=-14, rely=0.5, anchor="e")
            return b

        def sep() -> None:
            ttk.Separator(self).pack(fill="x", padx=14, pady=6)

        def head(text: str) -> None:
            ttk.Label(self, text=text, style="SideHead.TLabel").pack(anchor="w", padx=18, pady=(8, 2))

        item("Start screen", a.close_project)
        sep()
        item("New ETS2 project", lambda: a.new_project("ets2"), "Ctrl+N")
        item("New ATS project", lambda: a.new_project("ats"))
        item("Open…", a.open_dialog, "Ctrl+O")
        if a.project:
            sep()
            item("Save", a.save, "Ctrl+S")
            item("Save as…", a.save_as, "Ctrl+Shift+S")
            item("Close project", a.close_project)
            sep()
            item("Export…", lambda: a.export(ask=True), "F5")
            item("Export to mod folder", lambda: a.export(to_mod_folder=True), "Shift+F5")
        sep()
        item("Tutorials", a.open_tutorials, "F1")
        item("Settings…", a.open_settings)
        item("Open Paintbooth folder", lambda: open_path(appdirs.ensure()))
        item(f"About Paintbooth {__version__}", a.about)
        rec = [p for p in a.settings.get("recent", []) if Path(p).is_file()][:6]
        if rec:
            sep()
            head("Recent")
            for p in rec:
                b = item(Path(p).stem, partial(a.open_project, Path(p)))
                b.configure(style="SideSmall.TButton")
        sep()
        item("Exit", a.quit)

    def _run(self, cmd: Callable[[], object]) -> None:
        """Close the side bar, then run a menu command."""
        self.hide()
        cmd()

    def toggle(self) -> None:
        """Show the side bar, or hide it if shown."""
        if self.visible:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        """Build and show the side bar below the ribbon tabs, full window height."""
        self._build()
        r = self.app.root
        top = self.app.ribbon.winfo_y() + self.app.ribbon.tabs_frame.winfo_height()
        self.place(x=0, y=top, width=self.WIDTH, height=r.winfo_height() - top)
        self.lift()
        self.visible = True

    def hide(self) -> None:
        """Hide the side bar."""
        self.place_forget()
        self.visible = False

    def _click_anywhere(self, e: tk.Event) -> None:
        """Close the side bar on a click outside it (the File button toggles it itself)."""
        if not self.visible:
            return
        w = e.widget
        try:
            path = str(w)
        except (TypeError, tk.TclError):  # a click on a widget being destroyed
            return
        if path.startswith(str(self)) or w is self.app.ribbon.file_btn:
            return
        self.hide()
