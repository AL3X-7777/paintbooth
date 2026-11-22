"""Editor for Mods Studio 2 template items.

Those are the lightbox, windscreen decal, door window, billboard, cargo container V2, player company
logo and existing mod.

The form is built from the item's own variables (the same ones Mods Studio 2 shows), grouped
like MS2 groups them. Fields that don't apply to the current choice (e.g. accessory slot
patterns for a truck without them) are hidden.

Each field writes straight into the item's XML (``var_set`` and friends from :mod:`paintbooth.ms2`)
and then calls ``App.changed``. Image previews look files up through :func:`tplbuild.resolver`,
which can search the project folder, so they are debounced (:class:`~.editors.Debouncer`) rather
than run on every keystroke.

Links in the item's info tab come from template and project files, so only http(s) links are ever
opened (see :func:`is_web_url`).
"""

from __future__ import annotations

import functools
import tkinter as tk
import webbrowser
from collections.abc import Callable, Iterable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib.parse import urlsplit

from .. import ms2data, tplbuild
from ..ms2 import TemplateItem, var_choose, var_set, var_set_array
from ..ms2tpl import array_items, child, enabled, var_text
from ..xmldom import E, Elem
from .editors import PATH_DEBOUNCE_MS, Debouncer
from .widgets import CheckList, Mark, Preview, ScrollFrame, SearchCombobox, Section, Tooltip, open_path

GROUP_TITLES = {
    "basic": "Basic",
    "image": "Image",
    "images": "Images",
    "icon": "Shop icon",
    "Truck": "Truck",
    "cargo": "Cargo",
    "20ft_Container": "20 ft container",
    "40ft_Container": "40 ft container",
    "53ft_Container": "53 ft container",
    "Info": "Info",
}
HIDDEN_GROUPS = {"Enviroment", "environment"}  # sic: MS2 templates use both spellings
CARGO_HIDDEN = {"CargoGroup", "CargoBodyTypes", "CargoMaxDistance"}  # MS2 hides these too
IMAGE_TYPES = ("png", "dds", "jpg", "jpeg", "tga")
IMAGE_FILES = [("Images", "*.png *.dds *.jpg *.tga"), ("All files", "*.*")]

Lookup = Callable[[str], Elem | None]
OnChange = Callable[..., None]  # called as on_change() or on_change(True) ("the item's label changed")
Row = tuple[Elem, list[tk.Widget], Lookup]  # variable, its widgets, and the lookup for its conditions


# ---------------------------------------------------------------- links
def is_web_url(uri: str) -> bool:
    """True if ``uri`` is an absolute http or https URL with a host.

    Links in template info tabs come from files on disk (templates, projects people share), and
    ``webbrowser.open`` hands anything else to the OS: a UNC path or a local ``.lnk``/``.exe``
    would run when clicked. So everything except web links is refused.
    """
    try:
        parts = urlsplit(uri.strip())
    except ValueError:  # e.g. an invalid IPv6 host
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def open_web_url(uri: str) -> bool:
    """Open ``uri`` in the web browser if it is a web link (see :func:`is_web_url`).

    Returns:
        True if the browser was asked to open it.
    """
    if not is_web_url(uri):
        return False
    webbrowser.open(uri.strip())
    return True


def _open_link(uri: str, _event: object = None) -> None:
    """Click handler for a link label (Tk passes the event last)."""
    open_web_url(uri)


# ---------------------------------------------------------------- variables
def _lookup(vars_: Iterable[Elem]) -> Lookup:
    """Return a function that finds a variable by MS2 name, including dotted child names.

    A thin wrapper around :func:`paintbooth.tplbuild.var_lookup`, so the editor and export validation
    resolve names the same way.
    """
    return tplbuild.var_lookup(vars_)


def _option_label(o: Elem) -> str:
    """Text shown for one predefined option: ``"Group · Text"`` (or just the text/value)."""
    g, t = o.get_text("Group"), o.get_text("Text") or o.get_text("Value")
    return f"{g} · {t}" if g else t


def _allowed_extensions(v: Elem, default: Iterable[str]) -> list[str]:
    """The variable's ``AllowedExtensions`` list, or ``default`` if it has none."""
    allowed = v.find("AllowedExtensions")
    return [s.text for s in allowed.children if s.text] if allowed is not None else list(default)


def _image_size(v: Elem) -> tuple[str, str] | None:
    """``(width, height)`` from the variable's ImageSizeValidator, as text, if it has one."""
    size = None
    vals = v.find("Validators")
    for vd in vals.children if vals is not None else []:
        if vd.type() == "ImageSizeValidator":
            size = (vd.get_text("Width"), vd.get_text("Height"))
    return size


def _text_box(body: tk.Misc, height: int, wrap: str) -> tk.Text:
    """A bordered multi-line text field in the form's font."""
    return tk.Text(
        body,
        height=height,
        wrap=wrap,  # type: ignore[arg-type]  # "word"/"none"; typeshed wants a Literal
        borderwidth=1,
        relief="solid",
        highlightthickness=0,
        font="TkTextFont",
    )


class TemplateEditor(ttk.Frame):
    """The right-hand form for one :class:`~paintbooth.ms2.TemplateItem`.

    Uses these ``App`` attributes: ``project``, ``settings``, ``thumbs``, ``icons``, ``changed()``,
    ``image_dir()`` and ``remember_image_dir()``.
    """

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.item: TemplateItem | None = None
        self.loading = False  # True while load() fills the widgets: their callbacks must not write
        self.rows: list[Row] = []
        self.marks: dict[str, Mark] = {}
        head = ttk.Frame(self, padding=(8, 6))
        head.pack(fill="x")
        self.title = ttk.Label(head, text="", style="Head.TLabel")
        self.title.pack(side="left")
        self.folder_btn = ttk.Button(head, text="Open template folder", command=self._open_folder)
        self.folder_btn.pack(side="right")
        Tooltip(
            self.folder_btn,
            "Mods Studio 2's template folder for this item: PNG templates to paint on, "
            "and the files the mod is made from.",
        )
        self.desc = ttk.Label(self, text="", style="Muted.TLabel", wraplength=900, justify="left")
        self.desc.pack(fill="x", padx=10)
        self.problem = ttk.Label(self, text="", style="Err.TLabel", wraplength=900, justify="left")
        self.problem.pack(fill="x", padx=10, pady=(4, 0))
        self.sf = ScrollFrame(self)
        self.sf.pack(fill="both", expand=True, pady=(6, 0))

    # ---------------------------------------------------------------- context
    def ms2(self) -> Path | None:
        """The Mods Studio 2 install folder, or None if it was not found."""
        return tplbuild.ms2_dir(self.app.project) if self.app.project else None

    def tdir(self) -> Path | None:
        """MS2's template folder for the current item, or None."""
        return tplbuild.template_folder(self.item, self.ms2()) if self.item is not None else None

    def find(self, text: str) -> Path | None:
        """Resolve a path typed into a field the way the export will (may search the project)."""
        if self.app.project is None:
            return None
        return tplbuild.resolver(self.app.project, self.ms2(), self.tdir())(text)

    def _open_folder(self) -> None:
        """Open the template folder, or the selected truck/model's own sub-folder if there is one."""
        from .. import appdirs, glassdecal

        item = self.item
        if item is not None and item.type in glassdecal.ITEMS and glassdecal.is_native(item.get("Truck")):
            # Paintbooth's own templates
            kind = "Door windows" if item.type == "TruckDoorWindow" else "Windscreen decals"
            folder = appdirs.templates_dir("ets2") / kind
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
            return
        d = self.tdir()
        if d is None or self.item is None:
            messagebox.showinfo(
                "Template folder", "Mods Studio 2's template folder was not found on this PC."
            )
            return
        for v in self.item.variables():
            if v.type() == "ListItemVariable" and v.get_text("Value"):
                sub = ms2data.ci_path(d, v.get_text("Value"))
                if sub is not None and sub.is_dir():
                    open_path(sub)
                    return
        open_path(d)

    def _add_game_trucks(self, item: TemplateItem) -> None:
        """Windscreen decals and door windows: offer every ETS2 truck, not only Mods Studio 2's.

        The trucks Paintbooth builds itself from the game files are listed under
        "Paintbooth (from your game)" (see :mod:`paintbooth.glassdecal`).
        """
        from .. import glassdecal

        project = self.app.project
        if item.type not in glassdecal.ITEMS or project is None or project.game != "ets2":
            return
        trucks = sorted(
            (v.id, f"{v.brand} {v.name}".strip())
            for v in getattr(self.app, "library", {}).values()
            if v.game == "ets2" and not v.is_trailer and not v.is_bus and v.author == "SCS Software"
        )
        glassdecal.add_native_trucks(item.el, sorted(trucks, key=lambda t: t[1].lower()))

    # ---------------------------------------------------------------- building
    def load(self, item: TemplateItem) -> None:
        """Rebuild the whole form for ``item``."""
        self.loading = True
        self.item = item
        label, help_ = ms2data.FEATURES.get(item.type, (item.template_name, ""))
        self.title.configure(text=label)
        tab = item.el.find("InfoTab")
        info = [
            box.get_text("Content").strip()
            for box in (tab.children if tab is not None else [])
            if box.get_text("Content")
        ]
        self.desc.configure(text="\n".join([help_, *info]) if help_ else "\n".join(info))
        for w in self.sf.inner.winfo_children():
            w.destroy()
        self.rows, self.marks = [], {}
        self.drop_target: tuple[tk.StringVar, set[str]] | None = None
        self._add_game_trucks(item)
        vars_ = item.variables()
        lookup = _lookup(vars_)
        store = self.app.settings.setdefault("sections", {})
        for g, vs in self._groups(vars_).items():
            sec = Section(self.sf.inner, GROUP_TITLES.get(g, g.replace("_", " ").title()), f"tpl.{g}", store)
            sec.pack(fill="x", padx=4, pady=(4, 0))
            sec.body.columnconfigure(2, weight=1)
            r = 0
            for v in vs:
                r = self._row(sec.body, r, v, lookup, self.marks)
        self._links(tab)
        self.loading = False
        self.refresh_enabled()
        self.sf.to_top()
        self.after_idle(self.sf.to_top)

    @staticmethod
    def _groups(vars_: list[Elem]) -> dict[str, list[Elem]]:
        """The visible variables by MS2 group, in file order (environment and untitled ones hidden)."""
        groups: dict[str, list[Elem]] = {}
        for v in vars_:
            g = v.get_text("Group") or "basic"
            if g in HIDDEN_GROUPS or not v.get_text("Text") or v.get_text("Name").startswith("Env."):
                continue
            groups.setdefault(g, []).append(v)
        return groups

    def _links(self, tab: Elem | None) -> None:
        """Add a "Tutorial videos" section for the info tab's links, if there are any.

        Each link shows its URL as a tooltip. Links that are not http(s) are listed but can't be
        clicked (see :func:`is_web_url`).
        """
        links = []
        for box in tab.children if tab is not None else []:
            ls = box.find("Links")
            for link in ls.children if ls is not None else []:
                if link.get_text("Uri"):
                    links.append((link.get_text("Text") or link.get_text("Uri"), link.get_text("Uri")))
        if not links:
            return
        sec = Section(
            self.sf.inner,
            "Tutorial videos (online)",
            "tpl.links",
            self.app.settings.setdefault("sections", {}),
        )
        sec.pack(fill="x", padx=4, pady=(4, 0))
        for text, uri in links:
            if is_web_url(uri):
                lb = ttk.Label(sec.body, text=text, style="Link.TLabel", cursor="hand2")
                lb.bind("<Button-1>", functools.partial(_open_link, uri))
                Tooltip(lb, uri)
            else:
                lb = ttk.Label(sec.body, text=text, style="Muted.TLabel")
                Tooltip(lb, f"Not opened: only web links (http/https) can be opened.\n{uri}")
            lb.pack(anchor="w", padx=12, pady=1)

    def _row(
        self,
        body: ttk.Frame,
        r: int,
        v: Elem,
        lookup: Lookup,
        marks: dict[str, Mark],
        on_change: OnChange | None = None,
        rows: list[Row] | None = None,
    ) -> int:
        """Grid one labelled field for variable ``v`` at row ``r``.

        Args:
            body: The grid to add to (label | help | widget | mark columns).
            r: Grid row to use.
            v: The MS2 variable element.
            lookup: Resolves the names used by the variable's enable conditions.
            marks: Receives the problem mark under the variable's name.
            on_change: Called after the variable changed; default: mark the project changed.
            rows: Receives the row for :meth:`refresh_enabled`; default: ``self.rows``.

        Returns:
            The next free grid row.
        """
        on_change = on_change or self._changed
        name, title = v.get_text("Name"), v.get_text("Text")
        lab = ttk.Label(body, text=title + ":", anchor="e", width=22)
        lab.grid(row=r, column=0, sticky="ne", padx=(8, 6), pady=4)
        widget = self._widget(body, v, on_change)
        widget.grid(row=r, column=2, sticky="ew", pady=4)
        m = Mark(body)
        m.grid(row=r, column=3, sticky="n", padx=(4, 8), pady=4)
        marks[name] = m
        if v.get_text("QuickHelp"):
            Tooltip(lab, v.get_text("QuickHelp"))
        (self.rows if rows is None else rows).append((v, [lab, widget, m], lookup))
        return r + 1

    def _widget(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> tk.Widget:  # noqa: PLR0911
        """The input widget for one variable, chosen by its MS2 type (a dispatch table)."""
        t = v.type()
        if t in ("StringVariable", "IntegerVariable", "FloatVariable"):
            return self._entry(body, v, on_change, width=None if t == "StringVariable" else 12)
        if t == "TextVariable":
            return self._text(body, v, on_change)
        if t in ("BooleanVariable", "TwoOptionsVariable"):
            return self._bool(body, v, on_change)
        if t == "ListItemVariable":
            return self._choice(body, v, on_change)
        if t in ("ListItemArrayVariable", "StringArrayVariable", "ScsStringArrayVariable"):
            return self._checklist(body, v, on_change)
        if t in ("ScsImageSourceVariable", "ScsCargoIconVariable", "ImageSourceVariable"):
            return self._image(body, v, on_change)
        if t == "ScsImageSourceArrayVariable":
            return ImageList(body, self, v, on_change)
        if t == "FileSourceVariable":
            return self._file(body, v, on_change)
        if t in ("ScsEts2CargoArrayVariable", "ScsAtsCargoArrayVariable"):
            return CargoList(body, self, v, on_change)
        return ttk.Label(body, text="(this field can only be edited in Mods Studio 2)", style="Muted.TLabel")

    def _entry(self, body: ttk.Frame, v: Elem, on_change: OnChange, width: int | None = None) -> tk.Widget:
        """One-line text field; with ``width`` a short right-aligned number field."""
        var = tk.StringVar(value=v.get_text("Value"))

        def w(*_: object) -> None:
            if not self.loading:
                var_set(v, var.get())
                on_change(True)

        var.trace_add("write", w)
        if not width:
            return ttk.Entry(body, textvariable=var)
        fr = ttk.Frame(body)  # numbers: a short field on the left
        ttk.Entry(fr, textvariable=var, width=width, justify="right").pack(side="left")
        return fr

    def _text(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> tk.Text:
        """Multi-line text field (descriptions)."""
        txt = _text_box(body, 4, "word")
        txt.insert("1.0", v.get_text("Value"))

        def changed(_: object = None) -> None:
            if txt.edit_modified():
                txt.edit_modified(False)
                var_set(v, txt.get("1.0", "end-1c"))
                on_change()

        txt.bind("<<Modified>>", changed)
        return txt

    def _bool(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> ttk.Checkbutton:
        """Tick box; ticking can show or hide other fields, so their visibility is refreshed."""
        var = tk.BooleanVar(value=v.get_text("Value").lower() == "true")

        def flip() -> None:
            var_set(v, "true" if var.get() else "false")
            on_change()
            self.refresh_enabled()

        return ttk.Checkbutton(body, variable=var, command=flip)

    def _choice(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> SearchCombobox:
        """Searchable drop-down of the variable's predefined options (e.g. the truck)."""
        pre = v.find("PredefinedValues")
        opts = list(pre.children) if pre is not None else []
        labels = [_option_label(o) for o in opts]
        by_label = dict(zip(labels, opts, strict=True))
        cur = next(
            (lab for lab, o in by_label.items() if o.get_text("Value") == v.get_text("Value")),
            v.get_text("Value"),
        )
        var = tk.StringVar(value=cur)
        cb = SearchCombobox(body, textvariable=var, values=labels, height=20)
        cb.set_all(labels)

        def pick(_: object = None) -> None:
            o = by_label.get(var.get())
            if o is None or o.get_text("Value") == v.get_text("Value"):
                return
            var_choose(v, o.get_text("Value"))
            on_change(True)
            self.refresh_enabled()

        cb.bind("<<ComboboxSelected>>", pick)
        cb.bind("<Return>", pick)
        cb.bind("<FocusOut>", pick)
        return cb

    def _checklist(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> tk.Widget:
        """Multi-select list of predefined values; a plain one-per-line box if there are none."""
        pre = v.find("PredefinedValues")
        opts = list(pre.children) if pre is not None else []
        is_list = v.type() == "ListItemArrayVariable"
        if not opts and "Compan" in v.get_text("Name") and self.app.project:
            # companies: MS2's company list for this game
            opts = [_company_elem(c) for c in ms2data.companies(self.ms2(), self.app.project.game)]
        if not opts and not is_list:
            return self._lines(body, v, on_change)
        by_val = {o.get_text("Value"): o for o in opts}
        sel = [x if isinstance(x, str) else var_text(x) for x in array_items(v)]

        def changed(values: list[str]) -> None:
            if is_list:
                # ListItemArrayVariable stores copies of the chosen options as ListItemVariables
                out = []
                for val in values:
                    o = by_val.get(val)
                    if o is None:
                        continue
                    c = o.clone()
                    c.tag = "ListItemVariable"
                    c.attrs.pop("xsi:type", None)
                    out.append(c)
                var_set_array(v, out)
            else:
                var_set_array(v, list(values))
            on_change(True)

        cl = CheckList(body, on_change=changed, height=min(10, max(3, len(opts))), show_values=len(opts) > 12)
        cl.set_items(
            [
                (o.get_text("Value"), o.get_text("Text") or o.get_text("Value"), o.get_text("Group"))
                for o in opts
            ],
            sel,
        )
        return cl

    def _lines(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> tk.Text:
        """A string array without predefined values: one value per line, blank lines dropped."""
        txt = _text_box(body, 3, "none")
        txt.insert("1.0", "\n".join(x if isinstance(x, str) else var_text(x) for x in array_items(v)))

        def changed(_: object = None) -> None:
            if txt.edit_modified():
                txt.edit_modified(False)
                lines = [ln.strip() for ln in txt.get("1.0", "end").splitlines() if ln.strip()]
                var_set_array(v, lines)
                on_change()

        txt.bind("<<Modified>>", changed)
        return txt

    def _image(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> ttk.Frame:
        """Image path field with Browse, optional "Use default", a size hint and a preview."""
        fr = ttk.Frame(body)
        row = ttk.Frame(fr)
        row.pack(fill="x")
        var = tk.StringVar(value=v.get_text("Value"))
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        exts = _allowed_extensions(v, IMAGE_TYPES)
        default = tplbuild.default_image(v, self.tdir())
        size = _image_size(v)
        pw, ph = (512, 128) if size == ("256", "64") else (360, 200)  # shop icons get a wide preview
        prev = Preview(fr, self.app.thumbs, pw, ph)

        def show() -> None:
            p = self.find(var.get()) if var.get().strip() else default
            prev.show(p, "Default icon" if default is not None and not var.get().strip() else "No image")

        show_soon = Debouncer(fr, PATH_DEBOUNCE_MS, show)

        def browse() -> None:
            p = filedialog.askopenfilename(
                title=v.get_text("Text"),
                initialdir=self.app.image_dir(),
                filetypes=[("Images", " ".join(f"*.{x}" for x in exts)), ("All files", "*.*")],
            )
            if p:
                self.app.remember_image_dir(Path(p).parent)
                var.set(str(Path(p)))

        def w(*_: object) -> None:
            if not self.loading:
                var_set(v, var.get())  # the project changes at once; only the file lookup waits
                on_change()
                show_soon()

        var.trace_add("write", w)
        ttk.Button(row, text="Browse", command=browse).pack(side="left", padx=(6, 0))
        if default is not None:
            b = ttk.Button(row, text="Use default", command=lambda: var.set(""))
            b.pack(side="left", padx=(6, 0))
            Tooltip(b, f"Mods Studio 2's default ({default.name}). Used whenever this is empty.")
        hint = []
        if size:
            hint.append(f"{size[0]}×{size[1]} pixels")
        if default is not None:
            hint.append("empty = the template's default")
        if hint:
            ttk.Label(fr, text=", ".join(hint), style="Muted.TLabel").pack(anchor="w")
        prev.pack(anchor="w", pady=(4, 0))
        show()
        return fr

    def _file(self, body: ttk.Frame, v: Elem, on_change: OnChange) -> ttk.Frame:
        """File path field with Browse (the "Existing mod" .scs/.zip); files can be dropped on the window."""
        outer = ttk.Frame(body)
        fr = ttk.Frame(outer)
        fr.pack(fill="x")
        var = tk.StringVar(value=v.get_text("Value"))
        ttk.Entry(fr, textvariable=var).pack(side="left", fill="x", expand=True)
        exts = _allowed_extensions(v, ("scs", "zip"))

        def browse() -> None:
            p = filedialog.askopenfilename(
                title=v.get_text("Text"),
                filetypes=[("Mods", " ".join(f"*.{x}" for x in exts)), ("All files", "*.*")],
            )
            if p:
                var.set(str(Path(p)))

        def w(*_: object) -> None:
            if not self.loading:
                var_set(v, var.get())
                on_change(True)

        var.trace_add("write", w)
        ttk.Button(fr, text="Browse", command=browse).pack(side="left", padx=(6, 0))
        self.drop_target = (var, {f".{x.lower()}" for x in exts})
        ttk.Label(outer, text="or drag the file onto the Paintbooth window", style="Muted.TLabel").pack(
            anchor="w"
        )
        return outer

    def drop_file(self, path: Path) -> bool:
        """Put a file dragged onto the window into this item's file field; False if it has none."""
        target = getattr(self, "drop_target", None)
        if target is None or path.suffix.lower() not in target[1]:
            return False
        target[0].set(str(path))
        return True

    # ---------------------------------------------------------------- state
    def _changed(self, label: bool = False) -> None:
        """Default ``on_change``: tell the app (``label``: the item list text may have changed)."""
        if not self.loading:
            self.app.changed(label=label)

    def refresh_enabled(self) -> None:
        """Show or hide each field according to its MS2 enable conditions."""
        for v, ws, lookup in self.rows:
            on = enabled(v, lookup)
            for w in ws:
                (w.grid if on else w.grid_remove)()

    def show_problems(self, probs: list[Any]) -> None:
        """Set each field's mark from ``probs`` (``Problem``s); others go in the red text on top."""
        top = [p.message for p in probs if not p.field or p.field not in self.marks]
        self.problem.configure(text="\n".join(top[:6]))
        for k, m in self.marks.items():
            mine = [p for p in probs if p.field == k]
            if mine:
                m.set(
                    "error" if any(p.level == "error" for p in mine) else "warning",
                    "\n".join(p.message for p in mine),
                )
            else:
                m.set(None)


def _company_elem(c: Any) -> Elem:
    """A ListItemVariable option for one company from ``ms2data.companies``, shaped like MS2's."""
    return E(
        "ListItemVariable",
        Elem("ChildVariables", ""),
        E("Group", text=c.group),
        E("IsEnabled", text="true"),
        E("Text", text=c.text),
        Elem("Validators", ""),
        E("Value", text=c.value),
        E("Image", text=c.image),
        E("PredefinedValuesSelected", text="false"),
    )


class ImageList(ttk.Frame):
    """A list of images (billboards, logos, container skins): add, browse, reorder, remove."""

    def __init__(self, master: tk.Misc, editor: TemplateEditor, v: Elem, on_change: OnChange):
        super().__init__(master)
        self.editor, self.v, self.on_change = editor, v, on_change
        self.paths: list[str] = [x if isinstance(x, str) else var_text(x) for x in array_items(v)]
        self.box = ttk.Frame(self)
        self.box.pack(fill="x")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="Add image", command=lambda: self._add([""])).pack(side="left")
        b = ttk.Button(bar, text="Add files…", command=self._add_files)
        b.pack(side="left", padx=6)
        Tooltip(b, "Pick several images at once")
        self.hint = ttk.Label(bar, text="", style="Muted.TLabel")
        self.hint.pack(side="left", padx=8)
        self._render()

    def _limit(self) -> str:
        """The maximum number of images as text ("" if unlimited).

        MS2's ItemCountValidator gives either a fixed ``MaxCount`` or a ``MaxCountSource`` such as
        ``[BillboardModel.MaxCount]``, a variable of the current choice.
        """
        vals = self.v.find("Validators")
        for vd in vals.children if vals is not None else []:
            if vd.type() == "ItemCountValidator":
                src = vd.get_text("MaxCountSource")
                if src and self.editor.item is not None:
                    sv = _lookup(self.editor.item.variables())(src.strip("[]"))
                    if sv is not None and var_text(sv):
                        return var_text(sv)
                return vd.get_text("MaxCount")
        return ""

    def _store(self) -> None:
        """Write the list back into the variable and report a change."""
        var_set_array(self.v, list(self.paths))
        self.on_change(True)

    def _add(self, paths: list[str]) -> None:
        """Append ``paths`` ("" = an empty row to type into)."""
        self.paths += paths
        self._store()
        self._render()

    def _add_files(self) -> None:
        """Pick several images at once and append them."""
        ps = filedialog.askopenfilenames(
            title="Images", initialdir=self.editor.app.image_dir(), filetypes=IMAGE_FILES
        )
        if ps:
            self.editor.app.remember_image_dir(Path(ps[0]).parent)
            self._add([str(Path(p)) for p in ps])

    def _render(self) -> None:
        """Rebuild one row per image, and the "n of up to m" hint."""
        for w in self.box.winfo_children():
            w.destroy()
        for i, p in enumerate(self.paths):
            self._image_row(i, p)
        lim = self._limit()
        self.hint.configure(
            text=(f"{len(self.paths)} of up to {lim}" if lim else f"{len(self.paths)} image(s)")
        )

    def _image_row(self, i: int, p: str) -> None:
        """One row: number, preview, path field, Browse, move up/down and remove."""
        row = ttk.Frame(self.box, padding=(0, 2))
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=f"{i + 1}", width=3, anchor="e").pack(side="left")
        prev = Preview(row, self.editor.app.thumbs, 120, 60)
        prev.pack(side="left", padx=6)
        prev.show(self.editor.find(p) if p else None, "No image")
        var = tk.StringVar(value=p)
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)

        def show() -> None:
            prev.show(self.editor.find(var.get()) if var.get() else None, "No image")

        show_soon = Debouncer(row, PATH_DEBOUNCE_MS, show)

        def w(*_: object) -> None:
            self.paths[i] = var.get()
            var_set_array(self.v, list(self.paths))
            self.on_change()
            show_soon()

        var.trace_add("write", w)

        def browse() -> None:
            q = filedialog.askopenfilename(
                title="Image", initialdir=self.editor.app.image_dir(), filetypes=IMAGE_FILES
            )
            if q:
                self.editor.app.remember_image_dir(Path(q).parent)
                var.set(str(Path(q)))

        ttk.Button(row, text="Browse", command=browse).pack(side="left", padx=4)
        ic = self.editor.app.icons
        for icon, d, tip in (("up", -1, "Move up"), ("down", 1, "Move down")):
            b = ttk.Button(
                row,
                image=ic.get(icon, 16),
                style="Tool.TButton",
                command=functools.partial(self._move, i, d),
            )
            b.pack(side="left")
            Tooltip(b, tip)
        b = ttk.Button(row, image=ic.get("delete", 16), style="Tool.TButton", command=lambda: self._remove(i))
        b.pack(side="left")
        Tooltip(b, "Remove")

    def _move(self, i: int, d: int) -> None:
        """Swap image ``i`` with its neighbour (``d`` = -1 up, +1 down)."""
        j = i + d
        if 0 <= j < len(self.paths):
            self.paths[i], self.paths[j] = self.paths[j], self.paths[i]
            self._store()
            self._render()

    def _remove(self, i: int) -> None:
        """Delete image ``i``."""
        del self.paths[i]
        self._store()
        self._render()


class CargoList(ttk.Frame):
    """The cargos of a cargo container mod: start from one of the game's cargos, then edit.

    Each cargo is a ``Scs{Ets2,Ats}CargoVariable`` element with child variables; the selected one's
    children are shown below the list as an ordinary form (built with :meth:`TemplateEditor._row`).
    """

    def __init__(self, master: tk.Misc, editor: TemplateEditor, v: Elem, on_change: OnChange):
        super().__init__(master)
        self.editor, self.v, self.on_change = editor, v, on_change
        self.elem_tag = v.type().replace("ArrayVariable", "Variable")
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.tv = ttk.Treeview(top, show="tree", height=5, selectmode="browse")
        self.tv.pack(side="left", fill="x", expand=True)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show())
        side = ttk.Frame(top)
        side.pack(side="left", padx=(6, 0), anchor="n")
        game = editor.app.project.game if editor.app.project else "ets2"
        self.presets = ms2data.cargo_presets(editor.ms2(), game)
        self.preset_var = tk.StringVar()
        names = ["(empty cargo)", *(c.text for c in self.presets)]
        cb = SearchCombobox(side, textvariable=self.preset_var, width=24, values=names)
        cb.set_all(names)
        cb.pack(fill="x")
        Tooltip(cb, "Start from one of the game's cargos (its weight, value, companies...), then change it")
        ttk.Button(side, text="Add cargo", command=self._add).pack(fill="x", pady=(4, 0))
        ttk.Button(side, text="Remove", command=self._remove).pack(fill="x", pady=(4, 0))
        self.detail = ttk.Frame(self, padding=(0, 6))
        self.detail.pack(fill="x", pady=(6, 0))
        self.detail.columnconfigure(2, weight=1)
        self._render()

    def cargos(self) -> list[Elem]:
        """The cargo elements currently in the variable."""
        return [x for x in array_items(self.v) if isinstance(x, Elem)]

    @staticmethod
    def _label(c: Elem) -> str:
        """List text for one cargo: internal name and display name."""
        iname = var_text(child(c, "CargoInternalName")) or "(no internal name)"
        return f"  {iname}   {var_text(child(c, 'CargoName'))}"

    def _render(self, select: int | None = None) -> None:
        """Redraw the list and select cargo ``select`` (default: the first)."""
        self.tv.delete(*self.tv.get_children())
        for i, c in enumerate(self.cargos()):
            self.tv.insert("", "end", iid=str(i), text=self._label(c))
        kids = self.tv.get_children()
        if kids:
            pick = str(select) if select is not None and str(select) in kids else kids[0]
            self.tv.selection_set(pick)
        self._show()

    def _add(self) -> None:
        """Add a cargo copied from the chosen game cargo (or a blank one based on the first preset)."""
        choice = self.preset_var.get().strip()
        preset = next((c for c in self.presets if c.text == choice), None)
        base = preset if preset is not None else (self.presets[0] if self.presets else None)
        el = ms2data.cargo_preset(base) if base is not None else None
        if el is None:
            messagebox.showinfo(
                "Cargo", "Mods Studio 2's cargo list was not found, so a cargo can't be added here."
            )
            return
        el.tag = self.elem_tag
        for k in ("xmlns:xsi", "xmlns:xsd"):
            el.attrs.pop(k, None)
        # never reuse the game's own internal name (it would replace the game's cargo)
        iname = child(el, "CargoInternalName")
        if iname is not None:
            var_set(iname, "")
        cname = child(el, "CargoName")
        if cname is not None:
            var_set(cname, preset.text.title() if preset is not None else "")
        if preset is None:  # "(empty cargo)": keep the first preset's numbers but no companies
            for k in ("CargoCompaniesIn", "CargoCompaniesOut"):
                c = child(el, k)
                if c is not None:
                    var_set_array(c, [])
        cs = [*self.cargos(), el]
        var_set_array(self.v, cs)
        self.on_change(True)
        self._render(len(cs) - 1)

    def _remove(self) -> None:
        """Delete the selected cargo."""
        sel = self.tv.selection()
        if not sel:
            return
        cs = self.cargos()
        del cs[int(sel[0])]
        var_set_array(self.v, cs)
        self.on_change(True)
        self._render()

    def _show(self) -> None:
        """Build the form for the selected cargo's child variables (or a hint if none)."""
        for w in self.detail.winfo_children():
            w.destroy()
        sel = self.tv.selection()
        if not sel:
            ttk.Label(
                self.detail,
                text="Add a cargo: pick one of the game's cargos above as a starting point.",
                style="Muted.TLabel",
            ).grid(row=0, column=0, columnspan=4, sticky="w")
            return
        c = self.cargos()[int(sel[0])]
        cv = c.find("ChildVariables")
        kids = [k for k in (cv.children if cv is not None else []) if k.get_text("Name") not in CARGO_HIDDEN]
        lookup = _lookup(kids)
        hd = ttk.Frame(self.detail)
        ttk.Label(hd, text="Selected cargo", style="Section.TLabel").pack(side="left")
        ttk.Separator(hd).pack(side="left", fill="x", expand=True, padx=(10, 0))
        hd.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        r = 1
        marks: dict[str, Mark] = {}
        ed = self.editor
        was = ed.loading
        ed.loading = True  # building the form fills its fields; that must not count as an edit
        try:
            for k in kids:
                r = ed._row(
                    self.detail,
                    r,
                    k,
                    lookup,
                    marks,
                    on_change=self._child_changed,
                    rows=[],
                )
        finally:
            ed.loading = was

    def _child_changed(self, label: bool = False) -> None:
        """A field of the selected cargo changed: report it (the list label may change too)."""
        self.on_change(True)
        self._relabel()

    def _relabel(self) -> None:
        """Refresh the selected cargo's list text after its names were edited."""
        sel = self.tv.selection()
        if sel:
            self.tv.item(sel[0], text=self._label(self.cargos()[int(sel[0])]))
