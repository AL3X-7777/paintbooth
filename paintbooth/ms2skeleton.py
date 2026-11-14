r"""Create new MS2-format projects and items from scratch.

Used only when there is no existing project/item to copy from (see
:meth:`paintbooth.ms2.Ms2Project.new` and :meth:`~paintbooth.ms2.Ms2Project.add_like`).
Paintbooth's own structures carry the same variable names and types as MS2
project files, so Paintbooth can open, edit and export them; they do not include
MS2's build actions, which belong to Mods Studio 2's own templates.

Child element order matters: Mods Studio 2's XmlSerializer expects the children of
a variable in the order MS2 writes them (``ChildVariables``, ``EnabledSource``,
``Group``, ``IsEnabled``, ``Name``, ``Text``, ``Validators``, ``Value``, then the
type-specific elements). The builders below follow that order.

Paths such as ``\Data\ModProjects\ETS2\Templates\...`` are relative to the MS2
installation and use the game's folder name (``ATS`` or ``ETS2``).
"""

from __future__ import annotations

from .xmldom import E, Elem

# The mod categories the Mod Manager knows (manifest.sii ``category[]``).
CATEGORIES = [
    "truck",
    "trailer",
    "interior",
    "tuning_parts",
    "ai_traffic",
    "sound",
    "paint_job",
    "cargo_pack",
    "map",
    "ui",
    "weather_setup",
    "physics",
    "graphics",
    "models",
    "movers",
    "walkers",
    "prefabs",
    "other",
]


def _var(
    vtype: str,
    name: str,
    text: str,
    group: str,
    *extra: Elem,
    value: str | None = None,
    enabled_source: str | None = None,
    validators: list[Elem] | None = None,
) -> Elem:
    """Build an MS2 ``<Variable>`` of type ``vtype``.

    Args:
        vtype: The ``xsi:type`` (``StringVariable``, ``BooleanVariable``, ...).
        name: The variable name the exporter looks up.
        text: The label MS2 shows.
        group: The form group MS2 shows it in.
        *extra: Type-specific child elements, appended after ``<Value>``.
        value: The ``<Value>`` text; None leaves out the element.
        enabled_source: ``[OtherVariable]`` when another boolean enables this one.
        validators: ``<Validator>`` elements, if any.
    """
    kids = [Elem("ChildVariables", "")]
    if enabled_source:
        kids.append(E("EnabledSource", text=enabled_source))
    kids += [
        E("Group", text=group),
        E("IsEnabled", text="true"),
        E("Name", text=name),
        E("Text", text=text),
        E("Validators", *validators) if validators else Elem("Validators", ""),
    ]
    if value is not None:
        kids.append(Elem("Value", value))
    kids += list(extra)
    return Elem("Variable", None, {"xsi:type": vtype}, kids)


def _validator(vtype: str, *kids: Elem) -> Elem:
    """Build a ``<Validator xsi:type=...>`` element (written as ``<Validator ... />`` when empty)."""
    return Elem("Validator", None if kids else "", {"xsi:type": vtype}, list(kids))


def _color(
    name: str, text: str, group: str, rgb: tuple[float, float, float], enabled_source: str | None = None
) -> Elem:
    """Build an ScsColorVariable with a default linear colour (MS2 stores the channels B, G, R)."""
    return _var(
        "ScsColorVariable",
        name,
        text,
        group,
        E("Format", text="({0}, {1}, {2})"),
        E("B", text=str(rgb[2])),
        E("G", text=str(rgb[1])),
        E("R", text=str(rgb[0])),
        enabled_source=enabled_source,
    )


def _version() -> Elem:
    """Build the ``<MS2LibraryVersion>`` element MS2 writes into each project and item."""
    return E("MS2LibraryVersion", E("Major", text="2026"), E("Minor", text="1"))


def _string_array(name: str, text: str, group: str, items: list[tuple[str, str, str]]) -> Elem:
    """Build an ScsStringArrayVariable whose options are ``items`` as (group, text, value)."""
    pre = [
        Elem(
            "ListItemVariable",
            children=[Elem("ChildVariables", "")]
            + ([E("Group", text=g)] if g else [])
            + [
                E("IsEnabled", text="true"),
                E("Text", text=t),
                Elem("Validators", ""),
                E("Value", text=v),
                E("PredefinedValuesSelected", text="false"),
            ],
        )
        for g, t, v in items
    ]
    return _var(
        "ScsStringArrayVariable",
        name,
        text,
        group,
        E("AllowEditing", text="false"),
        E("DisplayMode", text="Auto"),
        E("InternalName", text=name),
        *([E("PredefinedValues", *pre)] if pre else []),
        E("PredefinedValuesSelected", text="false"),
    )


def new_manifest(dlcs: list[tuple[str, str, str]] | None = None, game: str = "ats") -> Elem:
    """Build an empty ``<ScsModManifest>``.

    ``compatible_versions`` starts empty on purpose: the exporter only writes
    ``compatible_versions[]`` lines the user typed in.

    Args:
        dlcs: Options for the DLC dependency list, as (group, text, value).
        game: ``"ats"`` or ``"ets2"``; selects the MS2 template folder of that game.
    """
    gdir = "ETS2" if game == "ets2" else "ATS"
    cats = [("", c, c) for c in CATEGORIES]
    variables = E(
        "Variables",
        _var("StringVariable", "display_name", "Mod Name", "basic", value=""),
        _var("StringVariable", "author", "Mod Author", "basic", value=""),
        _var("StringVariable", "package_version", "Mod Version", "basic", value=""),
        _string_array("dlc_dependencies", "DLC Dependencies", "basic", dlcs or []),
        _string_array("category", "Mod Category", "basic", cats),
        _string_array("compatible_versions", "Compatible Versions", "basic", []),
        _var(
            "ImageSourceVariable",
            "icon",
            "Mod Icon",
            "icon",
            E("AllowedExtensions", E("string", text="jpg"), E("string", text="png")),
            E("PreloadDefault", text="false"),
            value="",
        ),
        _var("TextVariable", "description", "Mod Description", "text", value=""),
        _var("BooleanVariable", "multiplayer_optional", "Multiplayer Optional", "multiplayer", value="false"),
        _var("StringVariable", "GlobalInternalName", "Mod Internal Name", "environment"),
    )
    return E(
        "ScsModManifest",
        Elem("BuildActions", ""),
        Elem("InfoTab", ""),
        _version(),
        E("TemplateBaseFolder", text=f"\\Data\\ModProjects\\{gdir}\\Templates\\Manifest\\"),
        E("TemplateDescription", text="Adds mod manifest data"),
        variables,
    )


def new_project(game: str = "ats") -> Elem:
    """Build the ``<Project>`` root of a new, empty project for ``game`` (``"ats"`` or ``"ets2"``)."""
    ats = game != "ets2"
    root = Elem(
        "Project",
        None,
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
            "xsi:type": "ScsProject",
        },
    )
    gdir = "ATS" if ats else "ETS2"
    root.children = [
        Elem("ModTemplates", ""),
        _version(),
        E("ProjectBaseFolder", text=f".\\Data\\ModProjects\\{gdir}\\"),
        E(
            "ProjectDescription",
            text="Mod project for American Truck Simulator by SCS Software"
            if ats
            else "Mod project for Euro Truck Simulator 2 by SCS Software",
        ),
        E("ProjectExportFileFilter", text="SCS files (*.scs)|*.scs|ZIP files (*.zip)|*.zip"),
        E("ProjectImage", text="header.jpg"),
        E("ProjectInternalName", text="scs_ats_mod" if ats else "scs_ets2_mod"),
        E("ProjectName", text="ATS Mod Project" if ats else "ETS2 Mod Project"),
        E("SteamId", text="270880" if ats else "227300"),
        E("TemplateLocation", text=".\\Templates\\"),
        new_manifest(game=game),
    ]
    return root


def new_advanced_truck_skin(game: str = "ats") -> Elem:
    """Build a new, empty Advanced Truck Skin item with MS2's default values.

    The defaults match Mods Studio 2's own template: price 1, white base colour,
    red/green/blue mask colours, metallic and mask options off.
    """
    gdir = "ATS" if game != "ets2" else "ETS2"
    truck = Elem(
        "Variable",
        None,
        {"xsi:type": "ScsVehicleVariable"},
        [
            Elem("ChildVariables", ""),
            E("Group", text="truck"),
            E("IsEnabled", text="true"),
            E("Name", text="Truck"),
            E("Text", text="Truck"),
            E(
                "Validators",
                _validator("NotEmptyValidator", E("AdviceMessage", text="Please select a value.")),
            ),
            Elem("AccessoryItems", ""),
            E("XmlSource", text="All"),
            E("Game", text=gdir),
            Elem("InternalName", ""),
            Elem("SoundItems", ""),
            Elem("VehicleAuthor", ""),
            Elem("VehicleImage", ""),
            Elem("VehicleName", ""),
            E("VehicleType", text="NewTrucks"),
            Elem("VehicleVariants", ""),
            Elem("XmlDefinitionAuthor", ""),
            Elem("DateUpdated", "", {"xsi:nil": "true"}),
        ],
    )

    def img_array(
        name: str, text: str, group: str, source: str, suitable_name: str, suitable_text: str
    ) -> Elem:
        """Build an ScsSuitableImageArrayVariable whose choices come from ``source`` (e.g. the cabins)."""
        return _var(
            "ScsSuitableImageArrayVariable",
            name,
            text,
            group,
            E("PredefinedValueSource", text=source),
            E("PredefinedValuesSelected", text="false"),
            E("SuitableForInternalName", text=suitable_name),
            E("SuitableForText", text=suitable_text),
            Elem("Value", ""),
        )

    mo, ma = "Metallic Options", "Mask Options"
    variables = E(
        "Variables",
        truck,
        _var("StringVariable", "InternalName", "Skin Internal Name", "basic", value=""),
        _var("StringVariable", "SkinName", "Skin Name", "basic", value=""),
        _var("IntegerVariable", "SkinPrice", "Skin Price", "basic", value="1"),
        _var("IntegerVariable", "SkinUnlock", "Unlock At Level", "basic", value="0"),
        _var("BooleanVariable", "AlternateUvSet", "Alternate UV", "basic", value="false"),
        _color("BaseColor", "Base Color", "basic", (1, 1, 1)),
        _var("BooleanVariable", "BaseColorLocked", "Base Color Locked", "basic", value="false"),
        _var(
            "ScsIconVariable",
            "SkinIcon",
            "Icon",
            "icon",
            E("AllowedExtensions", E("string", text="png"), E("string", text="dds")),
            E("PreloadDefault", text="false"),
            value="",
        ),
        img_array(
            "CabVariantSkin",
            "Cabin variant skins",
            "skins",
            "[Truck.VehicleVariants]",
            "suitable_for",
            "Suitable cabins",
        ),
        img_array(
            "AccessorySkins",
            "Accessories skins",
            "accessories",
            "[Truck.AccessoryItems]",
            "acc_list",
            "For accessories",
        ),
        _var("BooleanVariable", "EnableMetallic", "Enable Metallic Color", mo, value="false"),
        _color("FlipColor", "Flip Color", mo, (1, 0, 0), "[EnableMetallic]"),
        _color("FlakeColor", "Flake Color", mo, (0, 1, 0), "[EnableMetallic]"),
        _var(
            "FloatVariable",
            "FlipStrenght",
            "Flip Strenght",
            mo,
            E("FormatString", text="f2"),
            E("Step", text="0.1"),
            value="0.27",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "FloatVariable",
            "FlakeUVScale",
            "Flake UV Scale",
            mo,
            E("FormatString", text="f2"),
            E("Step", text="0.1"),
            value="32",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "FloatVariable",
            "FlakeDensity",
            "Flake Density",
            mo,
            E("FormatString", text="f2"),
            E("Step", text="0.1"),
            value="1",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "FloatVariable",
            "FlakeShininess",
            "Flake Shininess",
            mo,
            E("FormatString", text="f2"),
            E("Step", text="0.1"),
            value="50",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "FloatVariable",
            "FlakeClearcoat",
            "Flake Clearcoat",
            mo,
            E("FormatString", text="f2"),
            E("Step", text="0.1"),
            value="2.2",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "BooleanVariable",
            "FlipColorLocked",
            "Flip Color Locked",
            mo,
            value="true",
            enabled_source="[EnableMetallic]",
        ),
        _var(
            "BooleanVariable",
            "FlakeColorLocked",
            "Flake Color Locked",
            mo,
            value="true",
            enabled_source="[EnableMetallic]",
        ),
        _var("BooleanVariable", "EnableMask", "Enable Mask Color", ma, value="false"),
        _color("RedColor", "Red Mask Color", ma, (1, 0, 0), "[EnableMask]"),
        _color("GreenColor", "Green Mask Color", ma, (0, 1, 0), "[EnableMask]"),
        _color("BlueColor", "Blue Mask Color", ma, (0, 0, 1), "[EnableMask]"),
        _var(
            "BooleanVariable",
            "RedColorLocked",
            "Red Color Locked",
            ma,
            value="true",
            enabled_source="[EnableMask]",
        ),
        _var(
            "BooleanVariable",
            "GreenColorLocked",
            "Green Color Locked",
            ma,
            value="true",
            enabled_source="[EnableMask]",
        ),
        _var(
            "BooleanVariable",
            "BlueColorLocked",
            "Blue Color Locked",
            ma,
            value="true",
            enabled_source="[EnableMask]",
        ),
    )
    return Elem(
        "Template",
        None,
        {"xsi:type": "AdvancedTruckSkin"},
        [
            Elem("BuildActions", ""),
            Elem("InfoTab", ""),
            _version(),
            E("TemplateBaseFolder", text=f"\\Data\\ModProjects\\{gdir}\\Templates\\NewTruckSkin\\"),
            E("TemplateDescription", text="Create a truck skin using new skinning method"),
            E("TemplateDisplayPattern", text="[[Advanced Truck |]] {{Truck}} {{SkinName}}"),
            E("TemplateGroup", text="Truck Skin"),
            E("TemplateName", text="Advanced Truck Skin"),
            variables,
        ],
    )
