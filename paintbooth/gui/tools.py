"""SCS Tools ribbon windows: vehicle library manager, colour converter and TOBJ editor.

Each tool is a non-modal :func:`~paintbooth.gui.winutil.tool_window` opened from the Home ribbon
(``App.open_vehicle_manager``, ``App.open_color_converter``, ``App.open_tobj_editor``). They work on
files on disk, not on the open project, so every save path here reports IO errors in a dialog: a
failed save must never look like a successful one.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import struct
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, ClassVar

from .. import vehlib
from ..formats import read_tobj_path, tobj_bytes
from ..util import linear_to_srgb, srgb_to_linear
from .codeeditor import CodeEditor
from .icons import shared as icons_shared
from .theme import C
from .widgets import ColorRow, SearchCombobox, Tooltip, open_path
from .winutil import tool_window

log = logging.getLogger(__name__)


def _toplevel(app: Any, title: str, geometry: str, key: str | None = None) -> tk.Toplevel:
    """Open a tool window whose position is remembered under ``key`` (default: from the title)."""
    key = key or title.split(" - ", maxsplit=1)[0].lower().replace(" ", "_")
    return tool_window(app, key, title, geometry)


def _normalise_newlines(text: str) -> str:
    """Return ``text`` with CRLF and CR line ends turned into LF, as the Tk text widget uses."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------
# Vehicle library
# --------------------------------------------------------------------------


class VehicleManager:
    """Browse, edit (with a live syntax check) and scan the vehicle definition XML files.

    The left pane lists every vehicle of ``app.library`` grouped by game, kind and brand; the right
    pane is a :class:`CodeEditor` on the selected vehicle's file. Saving writes the file in its
    original encoding and reloads the library, so open projects pick up the change.

    Uses these ``App`` attributes: ``library``, ``library_dir``, ``library_errors``, ``project``,
    ``reload_library()``, ``scan_dialog()``, ``settings`` and ``vehicle_manager``.
    """

    def __init__(self, app: Any, select: str | None = None):
        self.app = app
        self.win = _toplevel(app, "Vehicles - Paintbooth", "1200x760", "vehicles")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._build_toolbar()
        pane = ttk.Panedwindow(self.win, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, padding=(8, 0, 4, 8))
        right = ttk.Frame(pane, padding=(4, 0, 8, 8))
        pane.add(left, weight=1)
        pane.add(right, weight=3)
        self._build_list(left)
        self._build_editor(right)
        self.win.bind("<Control-s>", lambda e: (self.save(), "break")[1])
        self.current: Path | None = None
        self.encoding = "utf-8"
        self.orig = ""  # text as last loaded or saved; differs from the editor when dirty
        self.render(select)
        if select:
            self._selected()

    def _build_toolbar(self) -> None:
        """Top row: scan, import, new vehicle, library folder and reload buttons."""
        app = self.app
        bar = ttk.Frame(self.win, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Scan game folder…", command=lambda: app.scan_dialog(folder=True)).pack(
            side="left"
        )
        ttk.Button(bar, text="Scan .scs / .zip…", command=lambda: app.scan_dialog(folder=False)).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="Import folder…", command=self.import_folder).pack(side="left")
        ttk.Button(bar, text="New vehicle", command=self.new_vehicle).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Open library folder", command=lambda: open_path(app.library_dir)).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="Reload", command=self._reload).pack(side="left")
        ttk.Label(bar, text=str(app.library_dir), style="Muted.TLabel").pack(side="right")

    def _build_list(self, left: ttk.Frame) -> None:
        """Left pane: filter box, vehicle tree and the list of files that failed to load."""
        sf = ttk.Frame(left)
        sf.pack(fill="x", pady=(0, 4))
        ttk.Label(sf, image=icons_shared().get("search", 14)).pack(side="left", padx=(0, 4))
        self.filter = tk.StringVar()
        fe = ttk.Entry(sf, textvariable=self.filter)
        fe.pack(side="left", fill="x", expand=True)
        Tooltip(fe, "Filter by name, internal name, brand or file name")
        self.filter.trace_add("write", lambda *a: self.render(self._selected_id()))
        self.tv = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tv.pack(fill="both", expand=True)
        self.tv.tag_configure("brand", font="SkBold")
        self.tv.tag_configure("err", foreground=C["err"])
        self.tv.bind("<<TreeviewSelect>>", self._selected)
        self.errors_lbl = ttk.Label(left, text="", style="Err.TLabel", wraplength=300, justify="left")
        self.errors_lbl.pack(fill="x", pady=(4, 0))

    def _build_editor(self, right: ttk.Frame) -> None:
        """Right pane: file label, Save button, editing tools, the XML editor and a status line."""
        top = ttk.Frame(right)
        top.pack(fill="x", pady=(0, 4))
        self.file_lbl = ttk.Label(top, text="Select a vehicle", style="Muted.TLabel")
        self.file_lbl.pack(side="left")
        self.save_btn = ttk.Button(
            top, text="Save  (Ctrl+S)", style="Accent.TButton", command=self.save, state="disabled"
        )
        self.save_btn.pack(side="right")
        tb = ttk.Frame(right)
        tb.pack(fill="x", pady=(0, 4))
        self.tool_btns = [
            ttk.Button(tb, text="+ Add accessory…", command=lambda: self.add_dialog("accessory")),
            ttk.Button(tb, text="+ Add cabin / body…", command=lambda: self.add_dialog("variant")),
            ttk.Button(tb, text="Find / replace  (Ctrl+F)", command=self._open_find),
            ttk.Button(tb, text="Go to line  (Ctrl+G)", command=self._ask_goto),
            ttk.Button(tb, text="Undo", command=lambda: self._edit("undo")),
            ttk.Button(tb, text="Redo", command=lambda: self._edit("redo")),
        ]
        for i, b in enumerate(self.tool_btns):
            b.pack(side="left", padx=(0 if i == 0 else 6, 0))
            b.state(["disabled"])
        self.editor = CodeEditor(right, on_change=self._changed)
        self.editor.pack(fill="both", expand=True)
        self.msg = ttk.Label(
            right,
            text="Saving reloads the library instantly; open projects pick up the change.",
            style="Muted.TLabel",
        )
        self.msg.pack(fill="x", pady=(4, 0))

    def _open_find(self) -> None:
        """Toolbar button: open the editor's find/replace bar."""
        self.editor.open_find()

    def _ask_goto(self) -> None:
        """Toolbar button: ask for a line number and jump to it."""
        self.editor.ask_goto()

    # ------------------------------------------------------------ list
    def _selected_id(self) -> str | None:
        """Library key of the selected tree row, or None for a brand row / no selection."""
        sel = self.tv.selection()
        return sel[0][3:] if sel and sel[0].startswith("v::") else None

    @staticmethod
    def _sort_key(kv: tuple[str, vehlib.Vehicle]) -> tuple[bool, str, str, str]:
        """Order vehicles: trucks before trailers, then by game, brand and display name."""
        v = kv[1]
        return (v.is_trailer, v.game, v.brand.lower(), v.display.lower())

    def render(self, select: str | None = None) -> None:
        """Redraw the vehicle tree (applying the filter) and the load-error list.

        Args:
            select: Library key of the vehicle to select and scroll to, if it is still listed.
        """
        self.tv.delete(*self.tv.get_children())
        q = self.filter.get().strip().lower()
        groups: dict[tuple[str, str, str], str] = {}
        for vid, v in sorted(self.app.library.items(), key=self._sort_key):
            hay = f"{v.display} {vid} {v.brand} {Path(v.source).name}".lower()
            if q and q not in hay:
                continue
            g = (
                {"all": "ATS + ETS2", "": "Any game"}.get(v.game, v.game.upper()),
                "Trailers" if v.is_trailer else "Trucks",
                v.brand or "Miscellaneous",
            )
            if g not in groups:
                groups[g] = self.tv.insert(
                    "", "end", text=f"{g[2]}   ·  {g[1]}, {g[0]}", open=True, tags=("brand",)
                )
            label = f"  {v.name or v.id}   ({v.id})"
            if vid != v.id:  # several files define this id: the key carries the file name
                label += f"   - {Path(v.source).name}"
            if not v.accessories:
                label += "   · no accessories"
            self.tv.insert(groups[g], "end", iid=f"v::{vid}", text=label)
        if select and self.tv.exists(f"v::{select}"):
            self.tv.selection_set(f"v::{select}")
            self.tv.see(f"v::{select}")
        errs = self.app.library_errors
        self.errors_lbl.configure(
            text=(
                "Files with problems:\n"
                + "\n".join(errs[:8])
                + (f"\n… and {len(errs) - 8} more" if len(errs) > 8 else "")
            )
            if errs
            else ""
        )
        if not self.app.library:
            self.file_lbl.configure(
                text="Your library is empty. Put vehicle XML files (Paintbooth or Mods Studio 2 "
                "format) in the library folder, scan the game, or use 'Save to library'."
            )

    def _selected(self, _: object = None) -> None:
        """Open the selected vehicle's file, asking first if the current one has unsaved edits."""
        vid = self._selected_id()
        if not vid:
            return
        v = self.app.library.get(vid)
        if v is None:
            return
        if self.current and Path(v.source) == self.current:
            return
        if (
            self.current is not None
            and self.dirty()
            and not messagebox.askyesno(
                "Unsaved changes", f"Discard changes to {self.current.name}?", parent=self.win
            )
        ):
            self.render(self._find_id(self.current))
            return
        self.open_file(Path(v.source), v.line)

    def _find_id(self, path: Path) -> str | None:
        """Library key of the vehicle defined in ``path`` (the first one, if it holds several)."""
        for vid, v in self.app.library.items():
            if Path(v.source) == path:
                return vid
        return None

    def open_file(self, path: Path, line: int = 0) -> bool:
        """Load ``path`` into the editor.

        Args:
            path: The vehicle XML file.
            line: 1-based line to scroll to (the vehicle's own element), or 0 for the top.

        Returns:
            False if the file could not be read (an error dialog was shown), else True.
        """
        try:
            text, encoding = vehlib.read_text(path)
        except OSError as e:
            messagebox.showerror("Vehicles", f"Can't read {path.name}:\n{e}", parent=self.win)
            return False
        self.current, self.encoding = path, encoding
        self.orig = _normalise_newlines(text)
        self.editor.set(self.orig)
        if line > 1:
            self.editor.goto(line, mark_error=False)
        fmt = (
            vehlib.xml_format(self.orig)
            .replace("ms2", "Mods Studio 2 format")
            .replace("paintbooth", "Paintbooth format")
        )
        self.file_lbl.configure(text=f"{path}   ·  {fmt}")
        self.save_btn.configure(state="disabled")
        for b in self.tool_btns:
            b.state(["!disabled"])
        self._check()
        return True

    def dirty(self) -> bool:
        """True if the editor text differs from the file as last loaded or saved."""
        return bool(self.current) and self.editor.get() != self.orig

    # ------------------------------------------------------------ editing
    def _edit(self, what: str) -> None:
        """Run the text widget's ``edit_undo``/``edit_redo``; an empty stack is not an error."""
        with contextlib.suppress(tk.TclError):  # nothing to undo/redo
            getattr(self.editor.text, f"edit_{what}")()

    def _changed(self) -> None:
        """Editor text changed: enable Save and re-check the XML once typing pauses."""
        self.save_btn.configure(state="normal" if self.dirty() else "disabled")
        self.editor._soon("check", 500, self._check)

    def _check(self) -> bool:
        """Live syntax check while typing (nothing is saved).

        Returns:
            True if the text parses as a vehicle definition.
        """
        text = self.editor.get()
        try:
            root = vehlib.parse_text(text)
        except ValueError as e:
            # vehlib.parse_text raises ValueError(message, line)
            self.editor.clear_error()
            self.editor.text.tag_add("errline", f"{e.args[1]}.0", f"{e.args[1]}.end+1c")
            self.msg.configure(text=f"Line {e.args[1]}: XML error - {e.args[0]}", style="Err.TLabel")
            return False
        self.editor.clear_error()
        if vehlib.is_ms2_vehicle(root):
            v = vehlib.from_ms2_node(root)
        else:
            found = [root] if root.tag == "vehicle" else root.find_all("vehicle")
            if not found:
                self.msg.configure(text="Not a vehicle file", style="Err.TLabel")
                return False
            n = found[0]
            vid = n.attrib.get("id", "")
            n_var, n_acc = len(n.find_all("variant")), len(n.find_all("accessory"))
            # a Paintbooth file carries no vehicle type here; the default ("NewTrucks") is a truck
            self._report_ok(vid, n_var, n_acc, vehlib.Vehicle(vid).is_trailer)
            return True
        self._report_ok(v.id, len(v.variants), len(v.accessories), v.is_trailer)
        return True

    def _report_ok(self, vid: str, n_variants: int, n_accessories: int, trailer: bool) -> None:
        """Status line for a file that parses: its id and how many cabins/bodies and accessories."""
        kind = "bodies" if trailer else "cabins"
        self.msg.configure(
            text=f"XML OK  ·  {vid or '(no id)'}  ·  {n_variants} {kind}, "
            f"{n_accessories} accessories" + ("  ·  unsaved changes" if self.dirty() else ""),
            style="Muted.TLabel",
        )

    def add_dialog(self, kind: str) -> None:
        """Open the Add accessory (``kind="accessory"``) or Add cabin/body (``"variant"``) dialog."""
        if not self.current:
            return
        AddEntryDialog(self, kind)

    def save(self) -> bool:
        """Write the editor text to the current file and reload the library.

        Returns:
            True if the file was written. On failure an error dialog is shown and the editor
            stays dirty, so the user can retry or copy the text elsewhere.
        """
        if not self.current:
            return False
        text = self.editor.get()
        try:
            vehlib.write_text(self.current, text, self.encoding)
        except (OSError, UnicodeEncodeError) as e:
            # UnicodeEncodeError: the file's own encoding (e.g. cp1252) can't hold a typed character
            log.warning("saving %s failed: %s", self.current, e)
            self.msg.configure(text=f"Not saved: {e}", style="Err.TLabel")
            messagebox.showerror("Vehicles", f"Could not save {self.current.name}:\n{e}", parent=self.win)
            return False
        self.orig = text
        self.save_btn.configure(state="disabled")
        self._report_reload(self.app.reload_library())
        return True

    def _report_reload(self, errors: list[str]) -> None:
        """Show the library errors that belong to the file just saved, or a success message."""
        assert self.current is not None
        rel = (
            self.current.relative_to(self.app.library_dir).as_posix()
            if self.current.is_relative_to(self.app.library_dir)
            else self.current.name
        )
        mine = [e for e in errors if e.startswith(rel)]  # errors look like "rel/path.xml:LINE: text"
        if mine:
            self.msg.configure(text="  ".join(mine), style="Err.TLabel")
            with contextlib.suppress(ValueError, IndexError):  # the error may have no line number
                self.editor.goto(int(mine[0].split(":")[1]))
        else:
            self.msg.configure(text=f"Saved {self.current.name} - library reloaded.", style="Muted.TLabel")

    def _reload(self) -> None:
        """Re-read every library file (after editing them outside Paintbooth)."""
        self.app.reload_library()
        self.msg.configure(text="Library reloaded.", style="Muted.TLabel")

    def import_folder(self) -> None:
        """Copy every vehicle XML (and an Images folder, if any) from a folder into the library."""
        picked = filedialog.askdirectory(parent=self.win, title="Folder with vehicle XML files")
        if not picked:
            return
        src = Path(picked)
        copied, skipped, failed = _import_vehicle_files(src, self.app.library_dir)
        self.app.reload_library()
        self.msg.configure(
            text=f"Imported {copied} file(s) from {src.name}"
            + (f", {skipped} already in the library" if skipped else "")
            + f"  ·  {len(self.app.library)} vehicles in the library",
            style="Err.TLabel" if failed else "Muted.TLabel",
        )
        if failed:
            messagebox.showerror(
                "Import folder",
                f"{len(failed)} file(s) could not be copied:\n" + "\n".join(failed[:10]),
                parent=self.win,
            )

    def new_vehicle(self) -> None:
        """Create a placeholder vehicle file in the library and open it for editing."""
        v = vehlib.Vehicle(
            "brand.model",
            self.app.project.game if self.app.project else "",
            "Brand",
            "Model",
            "Author",
            "you",
        )
        v.variants.append(vehlib.Variant("cabin_name.brand.model.cabin", "Cabin"))
        v.accessories.append(vehlib.Accessory("sideskirt.paint", "Paint", "Sideskirt"))
        try:
            vehlib.save_to_library(
                v, self.app.library_dir, "New vehicle - fill in the real ids from the game's def files"
            )
        except OSError as e:
            messagebox.showerror("New vehicle", f"Could not create the vehicle file:\n{e}", parent=self.win)
            return
        self.app.reload_library()
        self.render("brand.model")
        self._selected()

    def refresh(self) -> None:
        """The library was reloaded elsewhere: redraw, and reload the open file if it changed on disk.

        A file with unsaved edits is never replaced.
        """
        self.render(self._selected_id())
        if self.current and self.current.exists() and not self.dirty():
            try:
                text = _normalise_newlines(vehlib.read_text(self.current)[0])
            except OSError as e:
                log.warning("re-reading %s failed: %s", self.current, e)
                return
            if text != self.orig:
                self.orig = text
                self.editor.set(text, keep_view=True)

    def close(self) -> None:
        """Close the window, asking first if there are unsaved edits."""
        if (
            self.current is not None
            and self.dirty()
            and not messagebox.askyesno(
                "Unsaved changes", f"Close without saving {self.current.name}?", parent=self.win
            )
        ):
            return
        self.app.vehicle_manager = None
        self.win.destroy()


def _import_vehicle_files(src: Path, dest: Path) -> tuple[int, int, list[str]]:
    """Copy the vehicle XML files under ``src`` (and anything in a top-level Images folder) to ``dest``.

    A file of the same size already in ``dest`` counts as skipped; a different XML of the same name
    is copied as ``<name> (imported).xml`` so nothing in the library is overwritten.

    Returns:
        ``(copied, skipped, failed)``, where ``failed`` lists ``"relative/path: error"`` lines.
    """
    copied = skipped = 0
    failed: list[str] = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        if f.suffix.lower() != ".xml" and (not rel.parts or rel.parts[0].lower() != "images"):
            continue
        out = dest / rel
        try:
            if out.exists() and out.stat().st_size == f.stat().st_size:
                skipped += 1
                continue
            if out.exists() and f.suffix.lower() == ".xml":
                out = out.with_name(out.stem + " (imported).xml")
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
        except OSError as e:
            failed.append(f"{rel.as_posix()}: {e}")
            continue
        copied += 1
    return copied, skipped, failed


class AddEntryDialog:
    """Add an accessory (name, internal name, group) or a cabin/body to the open vehicle XML.

    The text is inserted into the editor (undoable); nothing is saved until you press Save.
    """

    def __init__(self, vm: VehicleManager, kind: str):
        self.vm, self.kind = vm, kind
        acc = kind == "accessory"
        title = "Add accessory" if acc else "Add cabin / body"
        w = self.win = tool_window(vm.app, f"add_{kind}", f"{title} - Paintbooth", resizable=False)
        w.transient(vm.win)
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        self.name = tk.StringVar()
        self.internal = tk.StringVar()
        self.group = tk.StringVar(value=vm.app.settings.get("last_acc_group", ""))
        self.desc = tk.StringVar()
        r = self._build_fields(f, acc)
        self.status = ttk.Label(f, text="", style="Muted.TLabel", wraplength=420, justify="left")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.grid(row=r + 1, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="Close", command=w.destroy).pack(side="right")
        ttk.Button(bar, text="Add", style="Accent.TButton", command=self.add).pack(side="right", padx=6)
        w.bind("<Return>", lambda e: self.add())
        w.bind("<Escape>", lambda e: w.destroy())
        self._refresh_groups()
        self.first.focus_set()

    def _build_fields(self, f: ttk.Frame, acc: bool) -> int:
        """Grid the name, internal name, group (accessories only) and description rows.

        Returns:
            The next free grid row.
        """
        rows = [
            ("Name", self.name, "Shown in the skin editor, e.g. 'Aero roof spoiler'"),
            (
                "Internal name",
                self.internal,
                "acc_list value, e.g. 'r_spoiler.aero_p'"
                if acc
                else "suitable_for value, e.g. 'day.brand.model.cabin' (trailers: 'body.curtain_137')",
            ),
        ]
        for r, (label, var, tip) in enumerate(rows):
            ttk.Label(f, text=label + ":").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            e = ttk.Entry(f, textvariable=var, width=44)
            e.grid(row=r, column=1, sticky="ew", pady=3)
            Tooltip(e, tip)
            if r == 0:
                self.first = e
        r = len(rows)
        if acc:
            ttk.Label(f, text="Group:").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            self.group_cb = SearchCombobox(f, textvariable=self.group, width=42)
            self.group_cb.grid(row=r, column=1, sticky="ew", pady=3)
            Tooltip(
                self.group_cb,
                "Type to search the groups (press Down to see the matches), or type a new group name",
            )
            self.group_hint = ttk.Label(f, text="", style="Muted.TLabel")
            self.group_hint.grid(row=r, column=2, sticky="w", padx=(6, 0))
            self.group.trace_add("write", lambda *a: self._group_hint())
            r += 1
        ttk.Label(f, text="Description:").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
        ttk.Entry(f, textvariable=self.desc, width=44).grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="optional", style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=(6, 0))
        return r + 1

    def _refresh_groups(self) -> None:
        """Offer the accessory groups found in the editor text (accessory dialog only)."""
        if self.kind == "accessory":
            self.group_cb.set_all(vehlib.accessory_groups(self.vm.editor.get()))
            self._group_hint()

    def _group_hint(self) -> None:
        """Say whether the typed group exists, how many groups match, or that it will be new."""
        g = self.group.get().strip()
        allg = self.group_cb._all
        if not g:
            self.group_hint.configure(text=f"{len(allg)} groups")
        elif g in allg:
            self.group_hint.configure(text="existing group")
        else:
            n = len([x for x in allg if g.lower() in x.lower()])
            self.group_hint.configure(text=f"{n} match(es) - Down to pick" if n else "new group")

    def add(self) -> None:
        """Insert the entry into the editor text, select it, and clear the fields for the next one."""
        name, internal = self.name.get().strip(), self.internal.get().strip()
        group, desc = self.group.get().strip(), self.desc.get().strip()
        if not internal:
            self.status.configure(text="Internal name is required.", style="Err.TLabel")
            return
        if not name:
            name = internal
        text = self.vm.editor.get()
        try:
            if self.kind == "accessory":
                if not group:
                    self.status.configure(text="Choose or type a group.", style="Err.TLabel")
                    return
                warn = internal in vehlib.accessory_values(text)
                edit = vehlib.add_accessory_edit(text, name, internal, group, desc)
            else:
                warn = False
                edit = vehlib.add_variant_edit(text, name, internal, desc)
        except ValueError as e:
            self.status.configure(text=str(e), style="Err.TLabel")
            return
        n_lines = edit.text.count("\n")
        self.vm.editor.replace_lines(
            edit.start, edit.end, edit.text, select=(edit.start, edit.start + n_lines)
        )
        self.vm.app.settings["last_acc_group"] = group
        msg = f"Added '{name}' at line {edit.start}."
        if warn:
            msg += f"  Note: '{internal}' was already in the file."
        self.status.configure(text=msg + "  Press Save in the editor to keep it.", style="Muted.TLabel")
        self.name.set("")
        self.internal.set("")
        self.desc.set("")
        self._refresh_groups()
        self.first.focus_set()


# --------------------------------------------------------------------------
# Colour converter
# --------------------------------------------------------------------------


def _sii_color(rgb: tuple[float, ...] | list[float]) -> str:
    """Format linear values the way .sii files write a colour: ``(0.1234, 0.5000, 1.0000)``."""
    return "(" + ", ".join(f"{c:.4f}" for c in rgb) + ")"


class ColorConverter:
    """Convert between a hex sRGB colour and the linear values the game's .sii files use."""

    def __init__(self, app: Any):
        self.win = _toplevel(app, "Color converter - Paintbooth", "640x220", "colorconv")
        f = ttk.Frame(self.win, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(
            f,
            text="Converts normal (sRGB) colours to the linear values the game's .sii files use, and back.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(f, text="Hex (sRGB):").grid(row=1, column=0, sticky="e", padx=6)
        self.hex = tk.StringVar(value="#ffffff")
        e = ttk.Entry(f, textvariable=self.hex, width=12)
        e.grid(row=1, column=1, sticky="w")
        e.bind("<KeyRelease>", lambda ev: self._from_hex())
        ttk.Label(f, text="SCS (linear):").grid(row=2, column=0, sticky="e", padx=6, pady=8)
        self.row = ColorRow(f, on_change=self._from_linear)
        self.row.grid(row=2, column=1, columnspan=3, sticky="ew")
        ttk.Label(f, text="Copy for .sii:").grid(row=3, column=0, sticky="e", padx=6)
        self.out = tk.StringVar()
        ttk.Entry(f, textvariable=self.out, width=40).grid(row=3, column=1, columnspan=2, sticky="ew")
        ttk.Button(f, text="Copy", command=self._copy).grid(row=3, column=3, sticky="w", padx=6)
        f.columnconfigure(2, weight=1)
        self.app = app
        self._from_hex()

    def _from_hex(self) -> None:
        """Hex box edited: update the linear row and the .sii text (ignored until 6 hex digits)."""
        h = self.hex.get().strip().lstrip("#")
        if len(h) == 6:
            try:
                lin = tuple(round(srgb_to_linear(int(h[i : i + 2], 16) / 255), 4) for i in (0, 2, 4))
            except ValueError:
                return
            self.row.set(lin)
            self.out.set(_sii_color(lin))

    def _from_linear(self, rgb: tuple[float, float, float]) -> None:
        """Linear row edited: update the hex box and the .sii text."""
        self.hex.set("#" + "".join(f"{round(linear_to_srgb(c) * 255):02x}" for c in rgb))
        self.out.set(_sii_color(rgb))

    def _copy(self) -> None:
        """Put the .sii text on the clipboard."""
        self.win.clipboard_clear()
        self.win.clipboard_append(self.out.get())


# --------------------------------------------------------------------------
# TOBJ editor
# --------------------------------------------------------------------------

# Binary .tobj layout (see paintbooth.formats): 0-3 magic, 4-19 unused, 20-39 texture settings
# (type, filters, mips, address modes...), 40-47 length of the first texture path, 48- the path,
# then any further paths (cube maps). The editor only changes the first path and, when the user
# picks another type, bytes 20-39. Everything else is kept from the opened file.
_TOBJ_SETTINGS = slice(20, 40)
_TOBJ_PATH_LEN = 40
_TOBJ_PATH = 48


def tobj_profile_of(data: bytes) -> str | None:
    """Return the name of the profile ("skin", "ui", "game") whose settings bytes ``data`` has.

    Returns:
        None if bytes 20-39 match no profile Paintbooth knows (filters, bias, cube maps...).
    """
    hdr = data[_TOBJ_SETTINGS]
    for name in TobjEditor.PROFILES:
        if tobj_bytes("/x", profile=name)[_TOBJ_SETTINGS] == hdr:
            return name
    return None


def rebuild_tobj(original: bytes | None, texture_path: str, profile: str | None) -> bytes:
    """Return a .tobj that points at ``texture_path``, keeping what the editor doesn't change.

    Args:
        original: Bytes of the opened file, or None for a new file.
        texture_path: Absolute game path, e.g. ``/vehicle/truck/upgrade/paintjob/x/skin.dds``.
        profile: Settings profile to write, or None to keep the original's bytes 20-39 unchanged.

    Raises:
        ValueError: The path is not absolute, or ``profile`` is None without an ``original``.
        UnicodeEncodeError: The path has non-ASCII characters (the game can't read them).
        struct.error: ``original`` is too short to be a .tobj.
    """
    fresh = tobj_bytes(texture_path, profile=profile or "skin")
    if original is None:
        if profile is None:
            raise ValueError("no original file to keep the settings from")
        return fresh
    old_len = struct.unpack_from("<I", original, _TOBJ_PATH_LEN)[0]
    tail = original[_TOBJ_PATH + old_len :]  # further paths (cube maps); empty for plain textures
    settings = original[_TOBJ_SETTINGS] if profile is None else fresh[_TOBJ_SETTINGS]
    return original[: _TOBJ_SETTINGS.start] + settings + fresh[_TOBJ_PATH_LEN:] + tail


class TobjEditor:
    """Open, edit and save the texture path and type of a binary .tobj file.

    A file whose settings match none of :attr:`PROFILES` gets an extra "keep" entry in the type list,
    selected by default, so saving it changes only the path.
    """

    PROFILES: ClassVar[dict[str, str]] = {
        "skin": "Paint job texture (DXT, mips)",
        "ui": "UI icon (uncompressed, no mips)",
        "game": "Generic game texture",
    }
    KEEP = "As in the file (custom settings, kept)"

    def __init__(self, app: Any):
        self.win = _toplevel(app, "TOBJ Editor - Paintbooth", "760x230", "tobj")
        self.original: bytes | None = None  # bytes of the opened file
        f = ttk.Frame(self.win, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="File:").grid(row=0, column=0, sticky="e", padx=6)
        self.file = tk.StringVar()
        ttk.Entry(f, textvariable=self.file).grid(row=0, column=1, sticky="ew")
        ttk.Button(f, text="Open…", command=self.open).grid(row=0, column=2, padx=6)
        ttk.Label(f, text="Texture path:").grid(row=1, column=0, sticky="e", padx=6, pady=8)
        self.path = tk.StringVar()
        ttk.Entry(f, textvariable=self.path).grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(f, text="Type:").grid(row=2, column=0, sticky="e", padx=6)
        cb = ttk.Combobox(f, state="readonly", values=list(self.PROFILES.values()), width=40)
        cb.current(0)
        cb.grid(row=2, column=1, sticky="w")
        self.cb = cb
        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Save as…", command=lambda: self.save(ask=True)).pack(side="right", padx=6)
        self.msg = ttk.Label(
            bar,
            text="Texture paths are absolute game paths, e.g. /vehicle/truck/upgrade/paintjob/…/skin.dds",
            style="Muted.TLabel",
        )
        self.msg.pack(side="left")
        f.columnconfigure(1, weight=1)

    def open(self) -> None:
        """Ask for a .tobj file and load it."""
        p = filedialog.askopenfilename(
            title="Open .tobj", filetypes=[("Texture objects", "*.tobj")], parent=self.win
        )
        if p:
            self.load(Path(p))

    def load(self, path: Path) -> bool:
        """Load ``path``: show its texture path and select its type.

        Returns:
            False if the file can't be read or is not a binary .tobj (an error dialog was shown).
        """
        try:
            data = path.read_bytes()
            tex = read_tobj_path(data)
        except (OSError, ValueError, struct.error) as e:  # UnicodeDecodeError is a ValueError
            messagebox.showerror("TOBJ", f"Can't read this file: {e}", parent=self.win)
            return False
        self.original = data
        self.path.set(tex)
        self.file.set(str(path))
        prof = tobj_profile_of(data)
        names = list(self.PROFILES.values())
        if prof is None:
            self.cb.configure(values=[*names, self.KEEP])
            self.cb.current(len(names))
        else:
            self.cb.configure(values=names)
            self.cb.current(list(self.PROFILES).index(prof))
        self.msg.configure(text=f"Loaded {path.name}", style="Muted.TLabel")
        return True

    def _profile(self) -> str | None:
        """The selected profile name, or None for "keep the file's settings"."""
        i = self.cb.current()
        return list(self.PROFILES)[i] if 0 <= i < len(self.PROFILES) else None

    def save(self, ask: bool = False) -> bool:
        """Write the .tobj to the File box's path (or ask for one).

        Args:
            ask: Always ask for the target file (Save as…).

        Returns:
            True if the file was written; on failure an error dialog is shown.
        """
        target = self.file.get()
        if ask or not target:
            target = filedialog.asksaveasfilename(
                defaultextension=".tobj", filetypes=[("Texture objects", "*.tobj")], parent=self.win
            )
            if not target:
                return False
        path = self.path.get().strip()
        if not path.startswith("/"):
            path = "/" + path.replace("\\", "/")
        return self._write(Path(target), path)

    def _write(self, target: Path, texture_path: str) -> bool:
        """Build and write the file, turning every failure into an error dialog."""
        try:
            data = rebuild_tobj(self.original, texture_path, self._profile())
        except UnicodeEncodeError:
            return self._fail("The texture path can only contain plain ASCII characters (no accents).")
        except (ValueError, struct.error) as e:
            return self._fail(f"Can't build the file: {e}")
        try:
            target.write_bytes(data)
        except OSError as e:
            return self._fail(f"Could not save {target.name}:\n{e}")
        self.file.set(str(target))
        self.msg.configure(text=f"Saved {target.name}", style="Muted.TLabel")
        return True

    def _fail(self, message: str) -> bool:
        """Show ``message`` as an error (status line and dialog) and return False."""
        self.msg.configure(text="Not saved", style="Err.TLabel")
        messagebox.showerror("TOBJ", message, parent=self.win)
        return False


__all__: list[str] = [
    "AddEntryDialog",
    "ColorConverter",
    "TobjEditor",
    "VehicleManager",
    "rebuild_tobj",
    "tobj_profile_of",
]
