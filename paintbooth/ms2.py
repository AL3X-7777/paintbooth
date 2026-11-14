"""Project files: Paintbooth .pb and Mods Studio 2 .ms2 (same format): open, edit, save.

A project is an :class:`~paintbooth.xmldom.Elem` tree of the file, wrapped by thin
accessors: :class:`Ms2Project` for the file, :class:`Item` subclasses for the
entries of ``<ModTemplates>`` and :class:`Vars` for an item's ``<Variables>``.
The accessors edit the tree in place and never rebuild it, so everything
Paintbooth does not understand is kept untouched, and an unchanged project
saves byte-for-byte identical to the original.

Export lives in :mod:`paintbooth.ms2build` (Advanced Truck Skin) and
:mod:`paintbooth.tplbuild` (Mods Studio 2 template items).

MS2 quirk to keep in mind throughout: Mods Studio 2's XmlSerializer reads elements
in a fixed order, so new elements must be inserted where MS2 itself puts them,
not appended. The ``before=`` arguments and ``_value_anchor``/``_value_index``
encode that order.
"""

from __future__ import annotations

import builtins
import re
import xml.parsers.expat
from collections.abc import Iterable, Sequence
from pathlib import Path, PureWindowsPath
from typing import ClassVar

from . import vehlib
from .xmldom import E, Elem, parse, serialize

ATS_STEAM_ID, ETS2_STEAM_ID = "270880", "227300"
# Paintbooth projects (.pb) are the same XML as Mods Studio 2 projects (.ms2): rename one to the other
# and either program opens it.
PROJECT_EXT = ".pb"
PROJECT_TYPES = [
    ("Projects (.pb, .ms2)", "*.pb *.ms2"),
    ("Paintbooth projects", "*.pb"),
    ("Mods Studio 2 projects", "*.ms2"),
]
SUPPORTED = {"AdvancedTruckSkin"}

# Longest internal names the game accepts. A skin's name gets a "_<i>" suffix per cab image on
# export, so skins are limited to 10 characters; other SCS tokens may use the full 12.
SKIN_NAME_LIMIT = 10
TOKEN_LIMIT = 12


def _b(text: str) -> bool:
    """Read an MS2 boolean (``true``/``false``, any case)."""
    return str(text).strip().lower() == "true"


def _bs(value: bool) -> str:
    """Write an MS2 boolean."""
    return "true" if value else "false"


def _num(text: str | float) -> str:
    """Format numbers like .NET does (0.6, 1, 20, 0.94)."""
    f = float(text)
    return str(int(f)) if f == int(f) else repr(round(f, 6)).rstrip("0").rstrip(".")


def _child(e: Elem, tag: str) -> Elem:
    """Return child ``tag`` of ``e``; raise ValueError when a damaged project lacks it."""
    c = e.find(tag)
    if c is None:
        raise ValueError(f"<{tag}> is missing in <{e.tag}>")
    return c


def _replace_value(owner: Elem, values: Iterable[str], after: str) -> None:
    """Replace ``owner``'s ``<Value>`` with a ``<string>`` list; a new one goes right after ``after``."""
    vals = list(values)
    new = E("Value", *[E("string", text=s) for s in vals]) if vals else Elem("Value", "")
    val = owner.find("Value")
    if val is None:
        idx = next((i + 1 for i, c in enumerate(owner.children) if c.tag == after), len(owner.children))
        owner.children.insert(idx, new)
    else:
        owner.children[owner.children.index(val)] = new


class Vars:
    """Access to a template's ``<Variables>`` by name.

    Every MS2 variable is a ``<Variable>`` (or similar) element with a ``<Name>`` child
    and, depending on its ``xsi:type``, a ``<Value>``, R/G/B channels, a string array,
    child variables, and so on. Setters silently do nothing for a variable that does
    not exist, because a project made by another MS2 version may lack it.
    """

    def __init__(self, owner: Elem):
        self.owner = owner

    @property
    def el(self) -> Elem:
        """The ``<Variables>`` element, created (empty) if the item has none."""
        v = self.owner.find("Variables")
        if v is None:
            v = Elem("Variables")
            self.owner.children.append(v)
        return v

    def get(self, name: str) -> Elem | None:
        """Return the variable element called ``name``, or None."""
        for v in self.el.children:
            if v.get_text("Name") == name:
                return v
        return None

    def value(self, name: str, default: str = "") -> str:
        """Return the ``<Value>`` text of variable ``name`` (``default`` if it is missing)."""
        v = self.get(name)
        return default if v is None else v.get_text("Value", default)

    def set_value(self, name: str, value: str) -> None:
        """Set the ``<Value>`` text of variable ``name``."""
        v = self.get(name)
        if v is not None:
            v.set_text("Value", value)

    # ``bool`` is a method name here (it mirrors MS2's BooleanVariable), so annotations inside
    # the class use ``builtins.bool``.
    def bool(self, name: str, default: builtins.bool = False) -> builtins.bool:
        """Return a BooleanVariable's value (``default`` if the variable is missing)."""
        v = self.get(name)
        return default if v is None else _b(v.get_text("Value", "false"))

    def set_bool(self, name: str, value: builtins.bool) -> None:
        """Set a BooleanVariable."""
        self.set_value(name, _bs(value))

    def color(self, name: str) -> tuple[float, float, float]:
        """Return an ScsColorVariable as linear (r, g, b); black if the variable is missing.

        Raises:
            ValueError: A channel is not a number. :func:`paintbooth.ms2build.validate`
                reports that as a problem before export.
        """
        v = self.get(name)
        if v is None:
            return (0.0, 0.0, 0.0)
        r, g, b = (float(v.get_text(c, "0") or 0) for c in "RGB")
        return (r, g, b)

    def set_color(self, name: str, rgb: Sequence[float]) -> None:
        """Set an ScsColorVariable from linear (r, g, b)."""
        v = self.get(name)
        if v is None:
            return
        for c, val in zip("RGB", rgb, strict=False):
            v.set_text(c, _num(val))
        # MS2 writes B, G, R order
        for tag in ("B", "G", "R"):
            e = _child(v, tag)
            v.children.remove(e)
            v.children.append(e)

    def predefined(self, name: str) -> list[tuple[str, str, str]]:
        """Return the (value, text, group) options stored with a list variable."""
        v = self.get(name)
        pre = v.find("PredefinedValues") if v is not None else None
        if pre is None:
            return []
        return [
            (c.get_text("Value"), c.get_text("Text") or c.get_text("Value"), c.get_text("Group"))
            for c in pre.children
        ]

    def child_value(self, name: str, child: str) -> str:
        """Return the value of child variable ``child`` of variable ``name`` (``""`` if missing)."""
        v = self.get(name)
        kids = v.find("ChildVariables") if v is not None else None
        for c in kids.children if kids is not None else []:
            if c.get_text("Name") == child:
                return c.get_text("Value")
        return ""

    def set_child_value(self, name: str, child: str, value: str) -> None:
        """Set child variable ``child`` of variable ``name``, adding it as a StringVariable if needed."""
        v = self.get(name)
        if v is None:
            return
        kids = v.find("ChildVariables")
        if kids is None:
            kids = Elem("ChildVariables")
            v.children.insert(0, kids)
        for c in kids.children:
            if c.get_text("Name") == child:
                c.set_text("Value", value)
                return
        kids.text = None
        kids.children.append(
            E(
                "Variable",
                Elem("ChildVariables", ""),
                E("IsEnabled", text="true"),
                E("Name", text=child),
                Elem("Validators", ""),
                Elem("Value", value),
            )
        )
        kids.children[-1].attrs["xsi:type"] = "StringVariable"

    def strings(self, name: str) -> list[str]:
        """Return the ``<string>`` entries of a string-array variable."""
        v = self.get(name)
        val = v.find("Value") if v is not None else None
        return [s.text or "" for s in val.children] if val is not None else []

    def set_strings(self, name: str, values: list[str]) -> None:
        """Replace the entries of a string-array variable (``<Value>`` goes after ``<Validators>``)."""
        v = self.get(name)
        if v is not None:
            _replace_value(v, values, after="Validators")


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


class Item:
    """One entry of ``<ModTemplates>``. This base class is used for item types Paintbooth cannot edit.

    Attributes:
        kind: The item type Paintbooth treats it as (``xsi:type`` for supported ones).
        el: The item's element in the project tree.
        vars: Access to its variables.
    """

    kind = "Unsupported"

    def __init__(self, el: Elem):
        self.el = el
        self.vars = Vars(el)

    @property
    def template_name(self) -> str:
        """The MS2 template name (``<TemplateName>``, or the ``xsi:type`` if absent)."""
        return self.el.get_text("TemplateName") or self.el.type()

    def _var_display(self, name: str) -> str:
        """Return how variable ``name`` is shown in an item title (option name, file name, ...)."""
        v = self.vars.get(name)
        if v is None:
            return ""
        if v.type() == "ScsVehicleVariable":
            return v.get_text("InternalName")
        if v.type() == "ListItemVariable":  # show the option's name, not its id
            pre = v.find("PredefinedValues")
            for o in pre.children if pre is not None else []:
                if o.get_text("Value") == v.get_text("Value"):
                    return o.get_text("Text") or v.get_text("Value")
        if v.type() == "FileSourceVariable":
            return PureWindowsPath(v.get_text("Value")).name
        return v.get_text("Value")

    def display(self) -> str:
        """Return the item's title for the project list, built like MS2 does.

        MS2's ``TemplateDisplayPattern`` uses ``[[text]]`` for literal text and
        ``{{Variable}}`` for variable values, e.g. ``[[Advanced Truck |]] {{Truck}} {{SkinName}}``.
        """
        pattern = self.el.get_text("TemplateDisplayPattern") or self.template_name
        s = re.sub(r"\[\[(.*?)\]\]", r"\1", pattern).replace("Advanced Truck |", "Advanced Skin |")
        s = re.sub(r"\{\{(.+?)\}\}", lambda m: self._var_display(m.group(1)), s)
        s = " ".join(s.split())
        head, bar, rest = s.partition("|")
        from .ms2data import FEATURES

        if self.el.type() in FEATURES:  # friendlier names ("Player company logo", not "PlayerCompanyLogo")
            head = FEATURES[self.el.type()][0]
        return f"{head.strip()} | {rest.strip()}" if bar and rest.strip() else head.strip()


class SuitableImage:
    """One image entry of a ScsSuitableImageArrayVariable (cab skin or accessory skin).

    The entry holds an ``<ImageSource>`` (the image path) and a ``<SuitableFor>`` list
    (the cabins or accessories the image is used for).
    """

    def __init__(self, el: Elem):
        self.el = el

    @property
    def image(self) -> str:
        """The image path as stored in the project (absolute or relative)."""
        v = self.el.path("ImageSource", "Value")
        return (v.text or "") if v is not None else ""

    @image.setter
    def image(self, value: str) -> None:
        _child(self.el, "ImageSource").set_text("Value", value, before="AllowedExtensions")

    @property
    def suitable(self) -> list[str]:
        """Internal names of the cabins/accessories this image is for."""
        val = self.el.path("SuitableFor", "Value")
        return [s.text or "" for s in val.children] if val is not None else []

    @suitable.setter
    def suitable(self, values: list[str]) -> None:
        _replace_value(_child(self.el, "SuitableFor"), values, after="DisplayMode")


def _list_item_var(
    text: str,
    value: str,
    image: str | None = None,
    group: str | None = None,
    description: str | None = None,
) -> Elem:
    """Build a ``<ListItemVariable>`` option (a cabin or accessory choice) in MS2's child order."""
    kids = [Elem("ChildVariables", "")]
    if description is not None:
        kids.append(E("Description", text=description))
    if group:
        kids.append(E("Group", text=group))
    kids += [
        E("IsEnabled", text="true"),
        E("Text", text=text),
        Elem("Validators", ""),
        E("Value", text=value),
    ]
    if image is not None:
        kids.append(E("Image", text=image))
    kids.append(E("PredefinedValuesSelected", text="false"))
    return Elem("ListItemVariable", children=kids)


def _validator(vtype: str, *kids: Elem) -> Elem:
    """Build a ``<Validator xsi:type=...>`` element."""
    return Elem("Validator", None, {"xsi:type": vtype}, list(kids))


def _new_suitable_entry(internal_name: str, predefined: list[Elem]) -> Elem:
    """Build an empty image entry, as MS2 creates it, for an array that has no entry to copy."""
    return E(
        "ScsSuitableImageVariable",
        Elem("ChildVariables", ""),
        E("IsEnabled", text="true"),
        Elem("Validators", ""),
        E(
            "ImageSource",
            Elem("ChildVariables", ""),
            E("IsEnabled", text="true"),
            E("Text", text="Image Path"),
            E(
                "Validators",
                _validator("NotEmptyValidator", E("AdviceMessage", text="Please select an image file.")),
                _validator("NPOTValidator", E("IsIcon", text="false")),
            ),
            Elem("Value", ""),
            E("AllowedExtensions", E("string", text="png"), E("string", text="dds")),
            E("PreloadDefault", text="false"),
        ),
        E(
            "SuitableFor",
            Elem("ChildVariables", ""),
            E("IsEnabled", text="true"),
            E("Text", text="Suitable variant"),
            E(
                "Validators",
                _validator("NotEmptyValidator", E("AdviceMessage", text="Please select a value.")),
            ),
            E("AllowEditing", text="false"),
            E("DisplayMode", text="Auto"),
            Elem("Value", ""),
            E("InternalName", text=internal_name),
            E("LineFormat", text='{0}[]: "{1}"'),
            E("PredefinedValues", *predefined) if predefined else Elem("PredefinedValues", ""),
            E("PredefinedValuesSelected", text="false"),
        ),
    )


def _clear_overlay(it: Item) -> None:
    """Empty the SkinIcon's ``OverlaySource`` child variable (the icon overlay image)."""
    v = it.vars.get("SkinIcon")
    cv = v.path("ChildVariables") if v is not None else None
    for c in cv.children if cv is not None else []:
        val = c.find("Value")
        if c.get_text("Name") == "OverlaySource" and val is not None:
            val.text = ""


class AdvancedTruckSkin(Item):
    """MS2's "Advanced Truck Skin" item: one paint job for one truck or ownable trailer.

    Variables used by the exporter: ``Truck`` (an ScsVehicleVariable holding the whole
    vehicle definition), ``InternalName``, ``SkinName``, ``SkinPrice``, ``SkinUnlock``,
    ``SkinIcon``, the colour variables in :attr:`COLOR_VARS`, the metallic and mask
    options, and two image arrays: ``CabVariantSkin`` and ``AccessorySkins``.
    """

    kind = "AdvancedTruckSkin"
    COLOR_VARS = ("BaseColor", "FlipColor", "FlakeColor", "RedColor", "GreenColor", "BlueColor")

    # ---- vehicle
    @property
    def vehicle_var(self) -> Elem | None:
        """The ``Truck`` variable element (MS2 stores the full vehicle definition in it)."""
        return self.vars.get("Truck")

    @property
    def truck_id(self) -> str:
        """The selected vehicle's internal name (``""`` when none is selected)."""
        v = self.vehicle_var
        return v.get_text("InternalName") if v is not None else ""

    def vehicle(self) -> vehlib.Vehicle | None:
        """Return the selected vehicle as stored in the project, or None."""
        v = self.vehicle_var
        if v is None or not v.get_text("InternalName"):
            return None
        return vehlib.from_ms2(v)

    def _predefined(self, array: str, veh: vehlib.Vehicle | None) -> list[Elem]:
        """Return the options of ``array``'s SuitableFor list for ``veh`` (cabins or accessories)."""
        if veh is None:
            return []
        if array == "CabVariantSkin":
            return [_list_item_var(x.name, x.id, x.image) for x in veh.variants]
        return [_list_item_var(a.name, a.value, a.image, a.group, a.description) for a in veh.accessories]

    def set_vehicle(self, veh: vehlib.Vehicle) -> None:
        """Select ``veh`` and refresh every image entry's choice list.

        Ticks that the new vehicle does not have (another truck's cabins) are removed.
        """
        var = self.vehicle_var
        if var is None:
            raise ValueError("this skin has no Truck variable")
        vehlib.apply_to_ms2(var, veh)
        for arr, valid in (
            ("CabVariantSkin", {x.id for x in veh.variants}),
            ("AccessorySkins", {a.value for a in veh.accessories}),
        ):
            pre = self._predefined(arr, veh)
            for entry in self.entries(arr):
                sf = _child(entry.el, "SuitableFor")
                old = sf.find("PredefinedValues")
                new = (
                    E("PredefinedValues", *[p.clone() for p in pre]) if pre else Elem("PredefinedValues", "")
                )
                if old is None:
                    # MS2 keeps PredefinedValues just before the last child (PredefinedValuesSelected)
                    sf.children.insert(len(sf.children) - 1, new)
                else:
                    sf.children[sf.children.index(old)] = new
                entry.suitable = [s for s in entry.suitable if s in valid]

    # ---- simple values
    def get(self, name: str, default: str = "") -> str:
        """Return variable ``name``'s value text."""
        return self.vars.value(name, default)

    def set(self, name: str, value: object) -> None:
        """Set variable ``name``'s value (converted with ``str``)."""
        self.vars.set_value(name, str(value))

    #: Variables only Paintbooth knows (bus windows and trim, see :mod:`paintbooth.coach`):
    #: name -> (MS2 variable type, label). They are added to a skin the first time they are set,
    #: so projects that never use them stay exactly as Mods Studio 2 wrote them.
    EXTRA_VARS: ClassVar[dict[str, tuple[str, str]]] = {
        "PaintWindows": ("BooleanVariable", "Paint windows and black trim"),
        "WindowsImage": ("StringVariable", "Windows and trim image"),
    }

    def set_extra(self, name: str, value: str) -> None:
        """Set one of :data:`EXTRA_VARS`, adding the variable first if the skin doesn't have it."""
        if self.vars.get(name) is None:
            from .ms2skeleton import _var

            vtype, label = self.EXTRA_VARS[name]
            self.vars.el.children.append(_var(vtype, name, label, "basic", value=""))
        self.vars.set_value(name, value)

    # ---- image arrays
    def entries(self, array: str) -> list[SuitableImage]:
        """Return the image entries of ``array`` (``CabVariantSkin`` or ``AccessorySkins``)."""
        v = self.vars.get(array)
        val = v.find("Value") if v is not None else None
        return [SuitableImage(e) for e in val.children] if val is not None else []

    def add_entry(self, array: str) -> SuitableImage:
        """Append an empty image entry to ``array`` and return it.

        The entry is a copy of the first existing entry (so it keeps the structure the
        project's MS2 version uses), or a fresh MS2-style entry if the array is empty.
        """
        v = self.vars.get(array)
        if v is None:
            raise ValueError(f"this skin has no {array} variable")
        val = v.find("Value")
        if val is None:
            val = Elem("Value")
            kids = v.children
            idx = next((i + 1 for i, c in enumerate(kids) if c.tag == "SuitableForText"), len(kids))
            v.children.insert(idx, val)
        if not val.children:
            val.text = None
        existing = val.children[0] if val.children else None
        if existing is not None:
            new = existing.clone()
            entry = SuitableImage(new)
            entry.image = ""
            entry.suitable = []
        else:
            iname = v.get_text("SuitableForInternalName") or (
                "suitable_for" if array == "CabVariantSkin" else "acc_list"
            )
            new = _new_suitable_entry(iname, self._predefined(array, self.vehicle()))
            entry = SuitableImage(new)
        val.children.append(new)
        return entry

    def remove_entry(self, array: str, index: int) -> None:
        """Remove image entry ``index`` from ``array``."""
        v = self.vars.get(array)
        if v is None:
            raise ValueError(f"this skin has no {array} variable")
        val = _child(v, "Value")
        del val.children[index]
        if not val.children:
            val.text = ""

    def sync_enabled(self) -> None:
        """Mirror MS2: variables with EnabledSource=[X] get IsEnabled = value of X."""
        for v in self.vars.el.children:
            src = v.get_text("EnabledSource")
            m = re.fullmatch(r"\[(\w+)\]", src or "")
            if m:
                v.set_text("IsEnabled", _bs(self.vars.bool(m.group(1))))


class TemplateItem(Item):
    """A Mods Studio 2 template item (lightbox, windscreen decal, billboard, ...).

    It is a form of variables plus build steps, built from the template files of the
    user's MS2 installation (see :mod:`paintbooth.tplbuild`).
    """

    kind = "Template"

    @property
    def type(self) -> str:
        """The item's ``xsi:type`` (the MS2 template class)."""
        return self.el.type()

    def var(self, name: str) -> Elem | None:
        """Return variable ``name``'s element, or None."""
        return self.vars.get(name)

    def variables(self) -> list[Elem]:
        """Return every ``<Variable>`` of the item, in file order."""
        v = self.el.find("Variables")
        return [c for c in v.children if c.tag == "Variable"] if v is not None else []

    def get(self, name: str, default: str = "") -> str:
        """Return variable ``name``'s value text."""
        return self.vars.value(name, default)

    def set(self, name: str, value: object) -> None:
        """Set variable ``name``'s value (placed where MS2 keeps it)."""
        v = self.vars.get(name)
        if v is not None:
            var_set(v, str(value))

    def set_choice(self, name: str, value: str) -> None:
        """Select option ``value`` of list variable ``name`` (see :func:`var_choose`)."""
        v = self.vars.get(name)
        if v is not None:
            var_choose(v, value)

    def array(self, name: str) -> list[str | Elem]:
        """Return the entries of array variable ``name`` (strings, or elements for cargo/list items)."""
        from .ms2tpl import array_items

        return array_items(self.vars.get(name))

    def set_array(self, name: str, values: list[str | Elem]) -> None:
        """Replace the entries of array variable ``name``."""
        v = self.vars.get(name)
        if v is not None:
            var_set_array(v, values)

    def internal_name(self) -> str:
        """Return the item's ``InternalName`` variable (``""`` if it has none)."""
        return self.get("InternalName")


def _value_anchor(v: Elem) -> str | None:
    """Return the tag a new ``<Value>`` of ``v`` must precede (MS2's element order), or None for the end."""
    for tag in (
        "AllowedExtensions",
        "PreloadDefault",
        "TrueValue",
        "PredefinedValues",
        "PredefinedValuesSelected",
        "InternalName",
        "LineFormat",
        "FormatString",
        "Step",
        "UseDefaultOptions",
    ):
        if v.find(tag) is not None:
            return tag
    return None


def _value_index(v: Elem) -> int:
    """Return where a new array ``<Value>`` goes in ``v`` (before the list details, after display options)."""
    for i, c in enumerate(v.children):
        if c.tag in ("AllowEditing", "DisplayMode"):
            continue
        if c.tag in (
            "PredefinedValues",
            "PredefinedValueSource",
            "PredefinedValuesSelected",
            "InternalName",
            "LineFormat",
            "AllowedExtensions",
        ):
            return i
    return len(v.children)


# ---- editing template variables (an item's own, or the fields of a cargo entry)


def var_set(v: Elem, value: str) -> None:
    """Set a variable's <Value> (placed where MS2 keeps it: after Validators, before list details)."""
    v.set_text("Value", value, before=_value_anchor(v))


def var_choose(v: Elem, value: str) -> None:
    """Select an option of a list variable.

    The option's child values (e.g. the truck's internal name) are copied along, like
    Mods Studio 2 does.
    """
    var_set(v, value)
    opt = None
    pre = v.find("PredefinedValues")
    for o in pre.children if pre is not None else []:
        if o.get_text("Value") == value:
            opt = o
    cv = v.find("ChildVariables")
    if cv is None:
        cv = Elem("ChildVariables")
        v.children.insert(0, cv)
    opt_kids = opt.find("ChildVariables") if opt is not None else None
    kids = [c.clone() for c in opt_kids.children] if opt_kids is not None else []
    cv.children = kids
    cv.text = None if kids else ""


def var_set_array(v: Elem, values: Sequence[str | Elem]) -> None:
    """Set an array variable.

    Strings are stored as ``<string>``, variable elements (cargo, list items) as they are.
    """
    kids = [E("string", text=x) if isinstance(x, str) else x for x in values]
    new = E("Value", *kids) if kids else Elem("Value", "")
    old = v.find("Value")
    if old is None:
        v.children.insert(_value_index(v), new)
    else:
        v.children[v.children.index(old)] = new


def template_supported(kind: str) -> bool:
    """Return True if Paintbooth can edit and build MS2 template items of type ``kind``."""
    from .ms2data import FEATURES

    return kind in FEATURES


ITEM_CLASSES: dict[str, type[Item]] = {"AdvancedTruckSkin": AdvancedTruckSkin}


def item_class(kind: str) -> type[Item]:
    """Return the accessor class for an item of ``xsi:type`` ``kind``."""
    if kind in ITEM_CLASSES:
        return ITEM_CLASSES[kind]
    return TemplateItem if template_supported(kind) else Item


def _raise_to_minimum(vars_el: Elem) -> None:
    """Start numbers that are below their NumberRangeValidator minimum at the minimum (e.g. price 0 -> 1)."""
    for v in vars_el.children:
        val = v.find("Validators")
        for vd in val.children if val is not None else []:
            if vd.type() == "NumberRangeValidator" and v.type() in ("IntegerVariable", "FloatVariable"):
                try:
                    lo = float(vd.get_text("MinValue"))
                    if float(v.get_text("Value") or 0) < lo:
                        v.set_text("Value", _num(lo))
                except ValueError:
                    pass  # a non-numeric minimum or value: leave it for the validator to report


def new_template_item(tdef_root: Elem, ms2_dir: Path | None) -> Elem:
    """Return a fresh project item made from a template.xml (as Mods Studio 2 stores it in a project).

    Args:
        tdef_root: Root element of the MS2 template definition.
        ms2_dir: The Mods Studio 2 installation folder. MS2 stores it in every item as the
            ``Env.ApplicationDirectory`` variable, which build steps use to find template files.
    """
    el = tdef_root.clone()
    el.tag = "Template"
    el.attrs = {"xsi:type": tdef_root.type()}
    vars_el = el.find("Variables")
    if vars_el is None:
        vars_el = Elem("Variables")
        el.children.append(vars_el)
    _raise_to_minimum(vars_el)
    if ms2_dir is not None and not any(
        v.get_text("Name") == "Env.ApplicationDirectory" for v in vars_el.children
    ):
        env = E(
            "Variable",
            Elem("ChildVariables", ""),
            E("Group", text="Enviroment"),  # sic: MS2's own spelling
            E("IsEnabled", text="true"),
            E("Name", text="Env.ApplicationDirectory"),
            Elem("Validators", ""),
            E("Value", text=str(PureWindowsPath(str(ms2_dir))).rstrip("\\") + "\\"),
        )
        env.attrs["xsi:type"] = "StringVariable"
        vars_el.children.append(env)
    return el


class Manifest(Item):
    """The project's ``<ScsModManifest>``: mod name, author, version, icon, description, categories."""

    kind = "ScsModManifest"

    def display(self) -> str:
        """Return the title shown in the project list."""
        return "Mod manifest"


# --------------------------------------------------------------------------
# Project
# --------------------------------------------------------------------------


class Ms2Project:
    """A .pb/.ms2 project file.

    Attributes:
        root: The ``<Project>`` element; all edits happen in this tree.
        path: Where the project was loaded from or last saved to (None for a new project).
    """

    def __init__(self, root: Elem, path: Path | None = None):
        self.root = root
        self.path = Path(path) if path else None
        self._saved = serialize(root)

    # ---- io
    @classmethod
    def new(cls, game: str = "ats") -> Ms2Project:
        """Return a new, unsaved project for ``game`` (``"ats"`` or ``"ets2"``)."""
        from .ms2skeleton import new_project

        p = cls(new_project(game))
        p._saved = b""  # a new project counts as unsaved
        return p

    @classmethod
    def load(cls, path: Path) -> Ms2Project:
        """Read a project file.

        Raises:
            OSError: The file cannot be read.
            xml.parsers.expat.ExpatError: It is not well-formed XML.
            ValueError: It is XML but not a project.
        """
        data = Path(path).read_bytes()
        root = parse(data)
        if root.tag != "Project":
            raise ValueError("not a Mods Studio 2 project (root element is not <Project>)")
        p = cls(root, path)
        p._saved = data
        return p

    def to_bytes(self) -> bytes:
        """Return the file contents for a save (after syncing MS2's derived ``IsEnabled`` flags)."""
        for it in self.items:
            if isinstance(it, AdvancedTruckSkin):
                it.sync_enabled()
        return serialize(self.root)

    def save(self, path: Path | None = None) -> Path:
        """Write the project atomically and return the path written.

        The new bytes are parsed once before the old file is replaced, so a save can
        never turn a working project into one that cannot be opened again.

        Raises:
            ValueError: No path was given and the project has none, or the serialized
                project does not parse (the old file is left untouched).
            OSError: The file cannot be written.
        """
        target = path or self.path
        if target is None:
            raise ValueError("the project has no file name yet")
        target = Path(target)
        data = self.to_bytes()
        try:
            parse(data)
        except xml.parsers.expat.ExpatError as e:
            raise ValueError(f"refusing to save a project that could not be opened again: {e}") from e
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError:
            tmp.unlink(missing_ok=True)  # don't leave half a file next to the project
            raise
        self.path = target
        self._saved = data
        return target

    @property
    def dirty(self) -> bool:
        """True if the tree differs from what was last loaded or saved."""
        return serialize(self.root) != self._saved

    # ---- info
    @property
    def game(self) -> str:
        """``"ets2"`` or ``"ats"``, from the Steam id (or the internal name of older projects)."""
        sid = self.root.get_text("SteamId")
        if sid == ETS2_STEAM_ID or "ets2" in self.root.get_text("ProjectInternalName").lower():
            return "ets2"
        return "ats"

    @property
    def title(self) -> str:
        """The project's name (``<ProjectName>``)."""
        return self.root.get_text("ProjectName") or "Mod Project"

    # ---- items
    @property
    def templates_el(self) -> Elem:
        """The ``<ModTemplates>`` element holding the items (created first in the file if missing)."""
        t = self.root.find("ModTemplates")
        if t is None:
            t = Elem("ModTemplates")
            self.root.children.insert(0, t)
        return t

    @property
    def items(self) -> list[Item]:
        """Accessors for every item, in project order. Indices match ``templates_el.children``."""
        return [item_class(t.type())(t) for t in self.templates_el.children]

    @property
    def manifest(self) -> Manifest:
        """The mod manifest (created empty if the project has none)."""
        m = self.root.find("ScsModManifest")
        if m is None:
            m = Elem("ScsModManifest", children=[Elem("Variables")])
            self.root.children.append(m)
        return Manifest(m)

    def move(self, index: int, to: int) -> int:
        """Move item ``index`` to position ``to`` (clamped); return the new position."""
        kids = self.templates_el.children
        to = max(0, min(len(kids) - 1, to))
        kids.insert(to, kids.pop(index))
        return to

    def delete(self, index: int) -> None:
        """Remove item ``index``."""
        del self.templates_el.children[index]

    def duplicate(self, index: int) -> int:
        """Insert a copy of item ``index`` after it, with a new unique internal name; return its index."""
        kids = self.templates_el.children
        new = kids[index].clone()
        kids.insert(index + 1, new)
        it = item_class(new.type())(new)
        if (
            isinstance(it, (AdvancedTruckSkin, TemplateItem))
            and it.vars.get("InternalName") is not None
            and it.get("InternalName")
        ):
            limit = SKIN_NAME_LIMIT if isinstance(it, AdvancedTruckSkin) else TOKEN_LIMIT
            it.set("InternalName", self.next_internal_name(it.get("InternalName"), limit=limit))
        return index + 1

    def add_like(self, kind: str, seed: Elem | None) -> int | None:
        """Add a new item of ``kind`` using ``seed`` as the template; return its index.

        Args:
            kind: The item's ``xsi:type``.
            seed: An item of that kind from any MS2 project. When None, an item of that kind
                in this project is used; failing that, Advanced Truck Skins are made from
                scratch and other kinds cannot be added (None is returned).
        """
        if seed is None:
            seed = next((t for t in self.templates_el.children if t.type() == kind), None)
        if seed is None:
            if kind != "AdvancedTruckSkin":
                return None
            from .ms2skeleton import new_advanced_truck_skin

            self.templates_el.children.append(new_advanced_truck_skin(self.game))
            return len(self.templates_el.children) - 1
        new = seed.clone()
        it = item_class(kind)(new)
        if isinstance(it, AdvancedTruckSkin):
            # new items start with empty text fields; numbers (price, unlock level) and colours are kept
            for name in ("InternalName", "SkinName", "SkinIcon"):
                if it.vars.get(name) is not None:
                    it.set(name, "")
            _clear_overlay(it)
            for arr in ("CabVariantSkin", "AccessorySkins"):
                for i in range(len(it.entries(arr)) - 1, -1, -1):
                    it.remove_entry(arr, i)
        self.templates_el.children.append(new)
        return len(self.templates_el.children) - 1

    def internal_names(self) -> list[str]:
        """Return the internal names of all skins and of template items that have one."""
        return [
            it.get("InternalName")
            for it in self.items
            if isinstance(it, AdvancedTruckSkin)
            or (isinstance(it, TemplateItem) and it.var("InternalName") is not None)
        ]

    def add_template(self, tdef_root: Elem, ms2_dir: Path | None, at: int | None = None) -> int:
        """Add a template item (lightbox, billboard, ...) made from an MS2 template.xml; return its index."""
        kids = self.templates_el.children
        idx = len(kids) if at is None else max(0, min(len(kids), at))
        kids.insert(idx, new_template_item(tdef_root, ms2_dir))
        self.templates_el.text = None
        return idx

    def next_internal_name(self, base: str, limit: int = TOKEN_LIMIT) -> str:
        """Return an internal name not used in the project, made by counting up ``base``'s number.

        ``skin07`` gives ``skin08``; a name without a number gets a 4-digit one
        (``myskin`` -> ``myskin0001``). The prefix is shortened when needed, so the
        result never exceeds ``limit`` characters (``abcdefghij99`` -> ``abcdefgh100``).

        Args:
            base: The name to start from.
            limit: The longest allowed name: :data:`SKIN_NAME_LIMIT` for skins,
                :data:`TOKEN_LIMIT` otherwise.
        """
        used = set(self.internal_names())
        m = re.match(r"^(.*?)(\d+)$", base or "")
        prefix, num, width = (
            (m.group(1), int(m.group(2)), len(m.group(2))) if m else ((base or "skin")[:6], 0, 4)
        )
        while True:
            num += 1
            digits = f"{num:0{width}d}"
            cand = prefix[: max(0, limit - len(digits))] + digits
            if cand not in used:
                return cand

    # ---- vehicles embedded in this project
    def embedded_vehicles(self) -> dict[str, vehlib.Vehicle]:
        """Return the vehicle definitions stored in the project's skins, by vehicle id (first one wins)."""
        out: dict[str, vehlib.Vehicle] = {}
        for it in self.items:
            if isinstance(it, AdvancedTruckSkin):
                v = it.vehicle()
                if v is not None:
                    out.setdefault(v.id, v)
        return out
