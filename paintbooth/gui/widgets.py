"""Reusable widgets for the Paintbooth window.

Small building blocks shared by the main window, the item editors and the tool windows:

* :class:`Tooltip`, :class:`Mark` (validation tick/cross) and :class:`Help` (a "?" with a tooltip);
* :class:`ScrollFrame`, :class:`Section` (collapsible) and :class:`CheckList` (filterable ticks);
* :class:`ColorRow` (game linear colour with a picker) and :class:`SearchCombobox`;
* :class:`Preview` with its background decoder :class:`ThumbCache`;
* :class:`UiQueue`, the only way worker threads may hand work back to Tk;
* :class:`Notice`, the "exported successfully" message, and :func:`open_path`.

The sRGB helpers are re-exported from :mod:`paintbooth.util` for the GUI modules that import them
from here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from tkinter import ttk
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageTk

from ..util import linear_to_srgb, srgb_to_linear
from .icons import shared as icons
from .theme import C

__all__ = [
    "CheckList",
    "ColorRow",
    "Help",
    "Mark",
    "Notice",
    "Preview",
    "ScrollFrame",
    "SearchCombobox",
    "Section",
    "ThumbCache",
    "Tooltip",
    "UiQueue",
    "linear_to_srgb",
    "open_path",
    "rgb_hex",
    "srgb_to_linear",
]

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]


def rgb_hex(lin: Iterable[float]) -> str:
    """Return the ``#rrggbb`` colour for a game linear colour (each channel 0-1)."""
    return "#" + "".join(f"{round(linear_to_srgb(float(v)) * 255):02x}" for v in lin)


class Tooltip:
    """A small yellow-box-style hint shown under a widget while the mouse is over it.

    Set ``.text`` to change the hint later; an empty text shows nothing.
    """

    def __init__(self, widget: tk.Misc, text: str = "") -> None:
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event: object = None) -> None:
        """Open the tip window just below the widget."""
        if not self.text or self.tip:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            background=C["raised"],
            foreground=C["fg"],
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=4,
            wraplength=420,
        ).pack()

    def _hide(self, _event: object = None) -> None:
        """Close the tip window, if open."""
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Mark(ttk.Label):
    """Validation indicator shown at the end of a field row (like MS2's check marks)."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, text="", anchor="center")
        self._blank = tk.PhotoImage(width=16, height=16)  # keeps the row height when no mark is shown
        self.configure(image=self._blank)
        self.tip = Tooltip(self)

    def set(self, level: str | None, message: str = "") -> None:
        """Show a mark for ``level`` (``"error"``, ``"warning"``, ``"ok"`` or None for none).

        Args:
            level: Problem level, or None to clear the mark.
            message: Tooltip text explaining the problem.
        """
        name = {"error": "mark_err", "warning": "mark_warn", "ok": "mark_ok"}.get(level or "")
        self.configure(image=icons().get(name, 16) if name else self._blank)
        self.tip.text = message


class Help(ttk.Label):
    """A boxed "?" whose tooltip explains the field next to it."""

    def __init__(self, master: tk.Misc, text: str) -> None:
        super().__init__(
            master, text="?", width=2, anchor="center", style="Muted.TLabel", borderwidth=1, relief="solid"
        )
        Tooltip(self, text)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame; put content into ``.inner``.

    The mouse wheel scrolls this frame only while the pointer is over it, and never steals the
    wheel from a list or text box inside it.
    """

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=C["bg"])
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind_all_wheel()

    def bind_all_wheel(self) -> None:
        """Route the wheel (Windows ``MouseWheel``, X11 buttons 4/5) here while the pointer is inside."""

        def on_enter(_event: object) -> None:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", self._wheel)
            self.canvas.bind_all("<Button-5>", self._wheel)

        def on_leave(_event: object) -> None:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

        self.bind("<Enter>", on_enter)
        self.bind("<Leave>", on_leave)

    def _wheel(self, e: tk.Event) -> None:
        """Scroll three units, unless the wheel is over an inner tree view or text box."""
        w: Any = e.widget
        while w is not None:
            if isinstance(w, (ttk.Treeview, tk.Text)):
                return
            if w is self:
                break
            w = getattr(w, "master", None)
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return
        delta = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")

    def to_top(self) -> None:
        """Scroll back to the top."""
        self.canvas.yview_moveto(0)


class ColorRow(ttk.Frame):
    """Three 0..1 spinboxes (game linear values), a Select button and a swatch."""

    def __init__(self, master: tk.Misc, on_change: Callable[[RGB], None] | None = None) -> None:
        super().__init__(master)
        self.on_change = on_change
        self.vars = [tk.StringVar() for _ in range(3)]
        self.spins: list[ttk.Spinbox] = []
        for i, v in enumerate(self.vars):
            sp = ttk.Spinbox(
                self,
                from_=0.0,
                to=1.0,
                increment=0.01,
                textvariable=v,
                width=10,
                justify="right",
                command=self._changed,
            )
            sp.grid(row=0, column=i, sticky="ew", padx=(0, 6))
            sp.bind("<KeyRelease>", lambda e: self._changed())
            sp.bind("<FocusOut>", lambda e: self._changed())
            self.spins.append(sp)
            self.columnconfigure(i, weight=1)
        self.btn = ttk.Button(self, text="Select", command=self._pick, style="Wide.TButton")
        self.btn.grid(row=0, column=3, sticky="ew", padx=(0, 6))
        self.columnconfigure(3, weight=1)
        self.swatch = tk.Frame(
            self, width=90, height=20, highlightthickness=1, highlightbackground=C["border"]
        )
        self.swatch.grid(row=0, column=4, sticky="nsew")
        self.columnconfigure(4, weight=1)
        self._mute = False  # True while set() fills the boxes, so on_change isn't called for it

    def get(self) -> RGB:
        """Return the three values, clamped to 0-1.

        Unreadable text counts as 0, and a comma works as the decimal point.
        """
        out = []
        for v in self.vars:
            try:
                out.append(max(0.0, min(1.0, float(v.get().replace(",", ".")))))
            except ValueError:
                out.append(0.0)
        return (out[0], out[1], out[2])

    def set(self, rgb: Sequence[float]) -> None:
        """Show a colour without calling ``on_change``."""
        self._mute = True
        for v, x in zip(self.vars, rgb, strict=False):
            v.set(f"{float(x):g}")
        self._mute = False
        self.swatch.configure(background=rgb_hex(rgb))

    def _changed(self) -> None:
        """Update the swatch and report the new colour."""
        rgb = self.get()
        self.swatch.configure(background=rgb_hex(rgb))
        if not self._mute and self.on_change:
            self.on_change(rgb)

    def _pick(self) -> None:
        """Open the colour picker (it works in sRGB 0-255) and convert the answer back to linear."""
        from .colorpick import ask_color  # colorpick imports this module

        cur = tuple(round(linear_to_srgb(c) * 255) for c in self.get())
        res = ask_color(cur, parent=self)
        if res:
            lin = tuple(round(srgb_to_linear(c / 255), 4) for c in res)
            self.set(lin)
            self._changed()

    def state(self, enabled: bool) -> None:  # type: ignore[override]  # an on/off switch, not ttk state flags
        """Enable or disable all the inputs."""
        for w in [*self.spins, self.btn]:
            w.state(["!disabled"] if enabled else ["disabled"])


class CheckList(ttk.Frame):
    """Filterable, grouped list of checkboxes (cabins / accessories / DLCs / categories).

    * ``show_values``: show the internal name after each entry ("Day Cab  (day.x.y.cabin)")
    * search by name, internal name or both
    * right-click menu: select all / none / invert (visible entries), and the same for one group

    Tree item ids are ``g::<group>`` for group rows and ``v::<value>`` for entries.
    """

    MODES = ("Name + internal", "Name", "Internal name")

    def __init__(
        self,
        master: tk.Misc,
        on_change: Callable[[list[str]], None] | None = None,
        height: int = 8,
        show_values: bool = False,
    ) -> None:
        super().__init__(master)
        self.on_change = on_change
        self.show_values = show_values
        self.items: list[tuple[str, str, str]] = []  # value, text, group
        self.selected: list[str] = []
        self.collapsed: set[str] = set()
        self.filter = tk.StringVar()
        self.mode = tk.StringVar(value=self.MODES[0])
        self._build_toolbar()
        self._build_tree(height)

    def _build_toolbar(self) -> None:
        """Filter box with placeholder, optional search-mode box, and All / None / Invert buttons."""
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 2))
        ent = ttk.Entry(top, textvariable=self.filter)
        ent.pack(side="left", fill="x", expand=True)
        self._ph = ttk.Label(
            top,
            text=" Filter…",
            image=icons().get("search", 14),
            compound="left",
            style="Muted.TLabel",
            background=C["field"],
        )
        self._ph.place(in_=ent, x=6, rely=0.5, anchor="w")
        self._ph.bind("<Button-1>", lambda e: ent.focus_set())
        ent.bind("<FocusIn>", lambda e: self._ph.place_forget())
        ent.bind(
            "<FocusOut>",
            lambda e: self._ph.place(in_=ent, x=6, rely=0.5, anchor="w") if not self.filter.get() else None,
        )
        self.filter.trace_add("write", lambda *a: self._render())
        if self.show_values:
            cb = ttk.Combobox(top, textvariable=self.mode, values=self.MODES, state="readonly", width=15)
            cb.pack(side="left", padx=(4, 0))
            cb.bind("<<ComboboxSelected>>", lambda e: self._render())
            Tooltip(
                cb,
                "What the filter searches: the display name, the internal name (e.g. bumper.aero_pt), "
                "or both",
            )
        for text, cmd, tip in (
            ("All", self.select_all, "Tick every entry shown (respects the filter)"),
            ("None", self.select_none, "Untick every entry shown (respects the filter)"),
            ("Invert", self.invert, "Flip every entry shown (respects the filter)"),
        ):
            b = ttk.Button(top, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=(4 if text == "All" else 0, 0))
            Tooltip(b, tip)

    def _build_tree(self, height: int) -> None:
        """The tree view with its scroll bar and mouse/keyboard bindings."""
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(body, show="tree", height=height, selectmode="none")
        sb = ttk.Scrollbar(body, command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv.pack(side="left", fill="both", expand=True)
        self.tv.tag_configure("group", font="SkBold")
        self.tv.tag_configure("stale", foreground=C["warn"])
        self.tv.bind("<Button-1>", self._click)
        self.tv.bind("<space>", self._key)
        for seq in ("<Button-3>", "<Button-2>" if sys.platform == "darwin" else "<Control-Button-1>"):
            self.tv.bind(seq, self._context)

    # ---------------------------------------------------------------- data
    def set_items(self, items: Iterable[tuple[str, str, str]], selected: Iterable[str]) -> None:
        """Replace the entries and the ticked values.

        Args:
            items: ``(value, text, group)`` triples; ``group`` may be empty. Duplicate values (from
                hand-edited XML) are dropped.
            selected: Values to show ticked. Values not among the items are still listed, marked
                "(not in this vehicle)", so they can be unticked.
        """
        seen = set()
        self.items = []
        for it in items:
            if it[0] not in seen:
                seen.add(it[0])
                self.items.append(it)
        self.selected = list(selected)
        self._render()

    def _matches(self, q: str, value: str, text: str, group: str) -> bool:
        """Return True if an entry matches the filter text ``q`` (lower case) in the chosen mode."""
        if not q:
            return True
        mode = self.mode.get()
        if mode == "Internal name":
            return q in value.lower()
        if mode == "Name":
            return q in text.lower() or q in (group or "").lower()
        return q in text.lower() or q in value.lower() or q in (group or "").lower()

    def _render(self) -> None:
        """Rebuild the tree from the items, the filter and the selection, keeping the scroll position."""
        tv = self.tv
        top = tv.yview()[0]
        tv.delete(*tv.get_children())
        q = self.filter.get().strip().lower()
        known = {v for v, _, _ in self.items}
        groups: dict[str, str] = {}
        for value, text, group in self.items:
            if not self._matches(q, value, text, group):
                continue
            parent = ""
            if group:
                if group not in groups:
                    is_open = q != "" or group not in self.collapsed  # a filter always opens groups
                    groups[group] = tv.insert(
                        "", "end", iid=f"g::{group}", text=group, open=is_open, tags=("group",)
                    )
                parent = groups[group]
            img = icons().get("chk_on" if value in self.selected else "chk_off", 16)
            label = f" {text}" + (f"   ({value})" if self.show_values and value != text else "")
            tv.insert(parent, "end", iid=f"v::{value}", text=label, image=img)
        # selections the vehicle no longer has
        for value in self.selected:
            if value not in known:
                tv.insert(
                    "",
                    "end",
                    iid=f"v::{value}",
                    text=f" {value}  (not in this vehicle)",
                    tags=("stale",),
                    image=icons().get("chk_on", 16),
                )
        tv.yview_moveto(top)

    def _changed(self) -> None:
        """Redraw and report the selection."""
        self._render()
        if self.on_change:
            self.on_change(list(self.selected))

    def _toggle(self, iid: str) -> None:
        """Open/close a group row, or tick/untick an entry row."""
        if iid.startswith("g::"):
            g = iid[3:]
            self.collapsed.symmetric_difference_update({g})
            self._render()
            return
        value = iid[3:]
        if value in self.selected:
            self.selected.remove(value)
        else:
            self.selected.append(value)
        self._changed()

    def _click(self, e: tk.Event) -> str:
        """Toggle the clicked row (anywhere on the row, not just the box)."""
        iid = self.tv.identify_row(e.y)
        if iid:
            self._toggle(iid)
        return "break"

    def _key(self, _event: tk.Event) -> None:
        """Space toggles the focused row."""
        iid = self.tv.focus()
        if iid:
            self._toggle(iid)

    # ---------------------------------------------------------------- bulk actions
    def _visible_values(self) -> list[str]:
        """Values of the entries currently shown (after filtering), including those in groups."""
        out = []
        for top in self.tv.get_children():
            for iid in [top, *self.tv.get_children(top)]:
                if iid.startswith("v::"):
                    out.append(iid[3:])
        return out

    def _group_values(self, group: str | None) -> list[str]:
        """All values in one group, whether shown or filtered out."""
        return [v for v, _, g in self.items if g == group]

    def _apply(self, values: Iterable[str], how: str) -> None:
        """Tick (``"all"``), untick (``"none"``) or flip (``"invert"``) ``values``, keeping the order."""
        values = list(values)
        if how == "all":
            for v in values:
                if v not in self.selected:
                    self.selected.append(v)
        elif how == "none":
            vs = set(values)
            self.selected = [v for v in self.selected if v not in vs]
        else:  # invert
            sel = set(self.selected)
            on = [v for v in values if v not in sel]
            off = {v for v in values if v in sel}
            self.selected = [v for v in self.selected if v not in off] + on
        self._changed()

    def _apply_group(self, group: str | None, how: str) -> None:
        """Bulk action on every entry of one group (see :meth:`_apply`)."""
        self._apply(self._group_values(group), how)

    def select_all(self) -> None:
        """Tick every entry shown."""
        self._apply(self._visible_values(), "all")

    def select_none(self) -> None:
        """Untick every entry shown."""
        self._apply(self._visible_values(), "none")

    def invert(self) -> None:
        """Flip every entry shown."""
        self._apply(self._visible_values(), "invert")

    def _context(self, e: tk.Event) -> str:
        """Right-click menu: bulk actions for the shown entries and for the clicked row's group."""
        iid = self.tv.identify_row(e.y)
        group = None
        if iid.startswith("g::"):
            group = iid[3:]
        elif iid:
            parent = self.tv.parent(iid)
            group = parent[3:] if parent.startswith("g::") else None
        m = tk.Menu(self, tearoff=False)
        shown = "filtered" if self.filter.get().strip() else "all"
        m.add_command(label=f"Select all ({shown})", command=self.select_all)
        m.add_command(label=f"Select none ({shown})", command=self.select_none)
        m.add_command(label=f"Invert selection ({shown})", command=self.invert)
        m.add_separator()
        st: Literal["normal", "disabled"] = "normal" if group else "disabled"
        name = f" '{group}'" if group else ""
        for label, how in (("Select all", "all"), ("Select none", "none"), ("Invert selection", "invert")):
            m.add_command(
                label=f"{label} in group{name}",
                state=st,
                command=partial(self._apply_group, group, how),
            )
        m.tk_popup(e.x_root, e.y_root)
        return "break"


class Section(ttk.Frame):
    """Collapsible section with a clickable header; put content in ``.body``.

    With ``key`` and ``store``, the open/closed state is remembered in ``store[key]``.
    """

    def __init__(
        self, master: tk.Misc, title: str, key: str | None = None, store: dict | None = None
    ) -> None:
        super().__init__(master)
        self.key, self.store = key, store
        self.header = ttk.Frame(self, cursor="hand2")
        self.header.pack(fill="x")
        self.arrow = ttk.Label(self.header, cursor="hand2")
        self.arrow.pack(side="left", padx=(4, 4))
        self.title = ttk.Label(self.header, text=title, style="Section.TLabel", cursor="hand2")
        self.title.pack(side="left")
        ttk.Separator(self.header).pack(side="left", fill="x", expand=True, padx=(10, 4))
        self.body = ttk.Frame(self)
        for w in (self.header, self.arrow, self.title):
            w.bind("<Button-1>", lambda e: self.toggle())
        self.open = not (store and key and store.get(key) is False)
        self._apply()

    def _apply(self) -> None:
        """Show or hide the body and update the arrow."""
        self.arrow.configure(image=icons().get("sec_open" if self.open else "sec_closed", 14))
        if self.open:
            self.body.pack(fill="both", expand=True, pady=(4, 8))
        else:
            self.body.pack_forget()

    def set_open(self, value: bool) -> None:
        """Open or close the section (and remember it, if a store was given)."""
        self.open = value
        if self.store is not None and self.key:
            self.store[self.key] = value
        self._apply()

    def toggle(self) -> None:
        """Flip between open and closed."""
        self.set_open(not self.open)


class UiQueue:
    """Hand results from worker threads to the Tk thread (Tk must only be used from one thread).

    Workers call :meth:`call` with a function; the Tk thread runs queued functions every 40 ms.
    """

    POLL_MS = 40

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.q: queue.Queue[Callable[[], object]] = queue.Queue()
        self._tick()

    def call(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` on the Tk thread soon. Safe to call from any thread."""
        self.q.put(fn)

    def _tick(self) -> None:
        """Run everything queued so far, then schedule the next poll."""
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # one failing callback must not stop the queue (and the app) for good
                log.exception("UI callback failed")
        with contextlib.suppress(tk.TclError):  # the window has been destroyed: stop polling
            self.root.after(self.POLL_MS, self._tick)


ThumbCallback = Callable[["Image.Image | None"], object]


class ThumbCache:
    """Decodes previews off the UI thread (8K PNGs take a while) and keeps the recent ones.

    The cache key is ``(path, mtime, box)``, so an image saved again in an editor is decoded anew.
    At most :attr:`MAX_ITEMS` thumbnails are kept (least recently used are dropped first), and
    requests for an image that is already being decoded wait for that decode instead of starting
    another thread.
    """

    MAX_ITEMS = 64

    def __init__(self, root: tk.Misc, ui: UiQueue) -> None:
        self.root = root
        self.ui = ui
        # Only touched on the Tk thread (workers hand their result over through ``ui``).
        self.cache: OrderedDict[tuple[str, int, tuple[int, int]], Image.Image | None] = OrderedDict()
        self.pending: dict[tuple[str, int, tuple[int, int]], list[ThumbCallback]] = {}

    def request(self, path: Path | None, box: tuple[int, int], callback: ThumbCallback) -> None:
        """Call ``callback`` with a thumbnail of ``path`` fitting ``box``, or None if it can't be read.

        The callback runs on the Tk thread: at once when the thumbnail is cached, otherwise when
        the background decode finishes.
        """
        if path is None:
            callback(None)
            return
        try:
            st = path.stat()
        except OSError:
            callback(None)
            return
        key = (str(path), st.st_mtime_ns, box)
        if key in self.cache:
            self.cache.move_to_end(key)
            callback(self.cache[key])
            return
        if key in self.pending:
            self.pending[key].append(callback)
            return
        self.pending[key] = [callback]
        threading.Thread(target=self._decode, args=(path, box, key), daemon=True).start()

    def _decode(self, path: Path, box: tuple[int, int], key: tuple[str, int, tuple[int, int]]) -> None:
        """Worker thread: decode and shrink the image, then hand it to the Tk thread."""
        img: Image.Image | None
        try:
            with Image.open(path) as im:
                im.draft("RGBA", box)  # lets JPEG decode at a reduced size
                small = im.convert("RGBA")
                small.thumbnail(box, Image.Resampling.BILINEAR)
                img = small.copy()
        except Exception as e:  # noqa: BLE001  # Pillow raises many types for damaged files
            # the preview just says it can't show the image
            log.info("can't preview %s: %s", path, e)
            img = None
        self.ui.call(lambda: self._finish(key, img))

    def _finish(self, key: tuple[str, int, tuple[int, int]], img: Image.Image | None) -> None:
        """Tk thread: store the result, drop the oldest entries, and answer every waiting request."""
        self.cache[key] = img
        while len(self.cache) > self.MAX_ITEMS:
            self.cache.popitem(last=False)
        for cb in self.pending.pop(key, []):
            cb(img)


class Preview(tk.Canvas):
    """Image preview on a checkerboard, scaled to fit."""

    def __init__(self, master: tk.Misc, thumbs: ThumbCache, width: int = 520, height: int = 260) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            highlightthickness=1,
            highlightbackground=C["border"],
            background=C["panel"],
        )
        self.thumbs, self.box = thumbs, (width, height)
        self._photo: ImageTk.PhotoImage | None = None
        self._path: Path | None = None

    def show(self, path: Path | None, empty_text: str = "No image") -> None:
        """Show ``path`` (decoded in the background), or ``empty_text`` when there is no image."""
        self._path = path
        self.delete("all")
        w, h = self.box
        self.create_text(w // 2, h // 2, text="Loading…" if path else empty_text, fill=C["muted"])

        def done(img: Image.Image | None, want: Path | None = path) -> None:
            # A newer show() call or a closed window makes this result obsolete.
            if want != self._path or not self.winfo_exists():
                return
            self.delete("all")
            if img is None:
                self.create_text(
                    w // 2,
                    h // 2,
                    text=empty_text if path is None else "Can't preview this image",
                    fill=C["muted"],
                )
                return
            self._photo = ImageTk.PhotoImage(_on_checkerboard(img))
            self.create_image(w // 2, h // 2, image=self._photo)

        self.thumbs.request(path, self.box, done)


def _on_checkerboard(img: Image.Image, tile: int = 8) -> Image.Image:
    """Composite an RGBA image over a grey checkerboard, so transparent areas are visible."""
    bg = Image.new("RGBA", img.size, (60, 62, 68, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, img.height, tile):
        for x in range((y // tile) % 2 * tile, img.width, tile * 2):
            d.rectangle((x, y, x + tile - 1, y + tile - 1), fill=(78, 80, 88, 255))
    bg.alpha_composite(img)
    return bg


def open_path(path: Path | str) -> None:
    """Show a file or folder in the system file manager.

    A file is selected in its folder on Windows; elsewhere its folder is opened.
    """
    path = Path(path)
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606  # opens a folder Paintbooth itself chose, not user input
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path.parent if path.is_file() else path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


class SearchCombobox(ttk.Combobox):
    """Editable combobox whose drop-down list narrows to the entries containing what you type.

    Type to filter, then press Down (or click the arrow) to choose from the matching entries.
    ``set_all(values)`` sets the full list. Entries that start with the text come first.
    """

    # Keys that move around the list rather than edit the text; they must not refilter.
    NAV = frozenset(
        {
            "Up",
            "Down",
            "Return",
            "KP_Enter",
            "Escape",
            "Tab",
            "ISO_Left_Tab",
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
            "Left",
            "Right",
            "Home",
            "End",
        }
    )

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._all: list[str] = list(kw.get("values", ()) or ())
        self.bind("<KeyRelease>", self._filter, add="+")
        self.bind("<FocusIn>", lambda e: self._filter(None), add="+")

    def set_all(self, values: Iterable[str]) -> None:
        """Set the full list of choices."""
        self._all = list(values)
        self._filter(None)

    def _filter(self, e: tk.Event | None) -> None:
        """Narrow the drop-down to the entries containing the current text."""
        if e is not None and e.keysym in self.NAV:
            return
        q = self.get().strip().lower()
        if not q or q in (v.lower() for v in self._all):
            vals = self._all
        else:
            starts = [v for v in self._all if v.lower().startswith(q)]
            vals = starts + [v for v in self._all if q in v.lower() and v not in starts]
        self.configure(values=vals)


class Notice:
    """A message box in the middle of the window with an OK button; any key press or click closes it too.

    ``link`` (text, callback) adds a clickable line, e.g. the export folder.
    """

    # A key still held from the shortcut that triggered the notice (F5) must not close it at once.
    GRACE_S = 0.35

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.frame: tk.Frame | None = None
        self.shown_at = 0.0
        root.bind_all("<KeyPress>", self._any, add="+")
        root.bind_all("<ButtonPress>", self._any, add="+")

    def show(self, title: str, message: str, link: tuple[str, Callable[[], object]] | None = None) -> None:
        """Show the notice (replacing one already shown).

        Args:
            title: Headline, in the success colour.
            message: Optional text below it.
            link: Optional ``(text, callback)``; clicking the text closes the notice and calls back.
        """
        self.hide()
        outer = tk.Frame(self.root, background=C["accent"], padx=1, pady=1)
        f = ttk.Frame(outer, style="Panel.TFrame", padding=(26, 18))
        f.pack()
        ttk.Label(f, text=title, style="NoticeHead.TLabel").pack(anchor="w")
        if message:
            ttk.Label(f, text=message, style="Notice.TLabel", wraplength=640, justify="left").pack(
                anchor="w", pady=(6, 0)
            )
        if link:
            text, cb = link
            lb = ttk.Label(
                f, text=text, style="NoticeLink.TLabel", cursor="hand2", wraplength=640, justify="left"
            )
            lb.pack(anchor="w", pady=(2, 0))

            def follow(_event: object) -> None:
                self.hide()
                cb()

            lb.bind("<Button-1>", follow)
            Tooltip(lb, "Open this folder")
        bar = ttk.Frame(f, style="Panel.TFrame")
        bar.pack(fill="x", pady=(12, 0))
        ttk.Label(bar, text="Press any key or click to close", style="NoticeHint.TLabel").pack(side="left")
        self.ok = ttk.Button(bar, text="OK", style="Accent.TButton", command=self.hide)
        self.ok.pack(side="right", padx=(16, 0))
        outer.place(relx=0.5, rely=0.42, anchor="center")
        outer.lift()
        self.frame = outer
        self.shown_at = time.monotonic()

    def hide(self) -> None:
        """Close the notice, if shown."""
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None

    def _any(self, _event: tk.Event) -> None:
        """Close on any key or click (the folder link's own click runs first and opens it)."""
        if self.frame is None or time.monotonic() - self.shown_at < self.GRACE_S:
            return
        self.hide()
