"""File > Tutorials: how to use Paintbooth, and the paint job modding basics behind it.

The topics are written in a tiny Markdown-like markup (see :data:`TOPICS`) and rendered into a
read-only Tk text widget by :class:`TutorialsWindow`. Everything is bundled, so the tutorials work
offline; the only online parts are the template-pack links, which open in the web browser and,
like every link Paintbooth opens, must be http(s) (:func:`~paintbooth.gui.tpleditor.open_web_url`).
"""

from __future__ import annotations

import functools
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Any

from .icons import shared as icons
from .theme import C
from .tpleditor import is_web_url, open_web_url
from .widgets import Tooltip
from .winutil import tool_window

# Markup: "# " title, "## " heading, "- " bullet, "1. " step, "> " tip, `code`, **bold**, *italic*,
# [text](https://url). Blank line = paragraph. Each source line is one rendered paragraph, so the
# prose is not wrapped in the source (hence the E501 exemptions on the strings).
# (section, title, body)
TOPICS: list[tuple[str, str, str]] = [
    (
        "Start here",
        "Quick start",
        """
# Quick start
Paintbooth turns your images into a paint job mod for Euro Truck Simulator 2 or American Truck Simulator. The short version:

1. **File > New ETS2 project** (or ATS).
2. Fill in the **Mod manifest**: mod name, author, version, description.
3. Click **Add Skin** on the Home tab. On the **Vehicle** tab, pick the truck or trailer.
4. On the **Skins** tab, click **Add image**, choose your painted image and tick the cabins (or trailer bodies) it is for.
5. Optional: on the **Accessories** tab, tick parts such as bumpers or mirrors and add an image or a plain colour for them.
6. On the **Main** tab, give the skin a name, price and shop icon.
7. Press **F5** to export. Press **Shift+F5** to export straight into the game's mod folder, then enable the mod in the game's Mod Manager.

> Everything works offline. Your projects, vehicles, templates and settings live in Documents\\Paintbooth.

The other topics explain each step in detail.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Start here",
        "Your first truck skin",
        """
# Your first truck skin, step by step
## 1. Get a template
A template is an image of the truck's paint layout (its "UV map"): where the doors, roof and front end are on the flat texture. You paint on top of it.

- Import a template pack once: **Settings > Templates > Import template pack (.zip)**.
- After you pick the vehicle, the **Skins** tab shows its template folder. **Open folder** opens it, so you can load the template in your image editor.

## 2. Paint
- Paint on layers above the template, and keep the template's guide lines on their own layer.
- When you are done, hide the guide layers and export the image as **PNG**, at a power-of-two size such as 4096×4096 (see *Images: sizes and formats*).

## 3. Make the project
1. **File > New ETS2 project** (or ATS).
2. **Mod manifest**: Mod Name and Author are what players see in the Mod Manager.
3. **Add Skin**, then pick the truck on the **Vehicle** tab. The list is grouped by brand; type in the search box to filter it.
4. **Skins** tab: **Add image**, then **Browse** to your PNG (or paste its path). Tick the cabins the image fits. Different cabins often need different images, so add one image per cabin layout if needed.
5. **Main** tab:
- **Skin Internal Name**: a short id using only a-z, 0-9 and _ (at most 10 characters here, because the game limits each part of a name to 12 and Paintbooth adds _0, _1… for each image).
- **Skin Name**: what the shop shows. **Price** and **Unlock At Level** work as for the game's own paint jobs.
- **Icon**: the small 256×64 shop picture. Paintbooth uses its default icon unless you choose one: **Browse** for an image, or **Use skin image** to make it from your skin. **Use default icon** goes back to the default.

## 4. Export and test
- **F5** builds the mod into Documents\\Paintbooth\\Exports\\ETS2 (change this in Settings).
- **Shift+F5** builds it straight into Documents\\Euro Truck Simulator 2\\mod.
- Start the game, open the **Mod Manager** on the profile screen, enable your mod, and load the profile. The paint job is offered where the game lets you change your truck's paint.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Start here",
        "The Paintbooth window",
        """
# The Paintbooth window
- **File** opens the side bar: new/open/save, export, settings, tutorials, recent projects and the start screen.
- **Home** ribbon: project buttons, the item buttons (Mod manifest, Add Skin, Duplicate, Delete) and export.
- **Home** ribbon, between Export and Program: Color converter, TOBJ editor and the **Vehicles** library.
- **Make the ribbon yours**: drag a button onto another button (it goes in front of it) or onto a group's name (it goes at the end of that group). Rest the pointer on **Add more** to open its list, and drag an entry (Truck lightbox, Billboard…) onto the ribbon to give it a button of its own. Right-click a button or a group name to move it, make a new group, rename or remove; **Reset ribbon** there (or **Settings > Reset to defaults**) puts everything back.
- Left: the project's items. **Mod manifest** is always first; each Advanced Skin is one paint job.
- Right: the selected item. An Advanced Skin has four tabs: **Vehicle**, **Main**, **Skins**, **Accessories**.
- Bottom: the status bar. The counter on the right lists problems; click it to see them all. Fields with a problem get a red or orange mark; hover over the mark for details.

> Can't see the item list? Press **Ctrl+M** for the mod manifest, or drag the divider at the left edge to the right.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Painting",
        "Templates",
        """
# Templates
## Importing a template pack
1. Download the template pack for your game (a .zip) from SCS's forum:
   - ETS2: [ETS2 truck & trailer templates](https://forum.scssoft.com/viewtopic.php?t=272386)
   - ATS: [ATS truck & trailer templates](https://forum.scssoft.com/viewtopic.php?t=288778)
2. **Settings > Templates > Import template pack (.zip)…**. It is unpacked into Documents\\Paintbooth\\Templates\\ETS2 (or ATS). Importing the same pack again only copies what changed.

You can also unzip a pack into that folder yourself.

## Finding a vehicle's templates
Paintbooth matches vehicles to template folders by name (internal name, in-game name, and the name in the vehicle XML). The **Skins** tab shows the folder it found:
- **Open folder** opens it in Explorer.
- **Choose folder…** sets the folder by hand if the match is wrong or missing. Your choice is remembered for that vehicle.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Painting",
        "Getting template packs",
        """
# Getting template packs
Templates are images that show where each part of a truck or trailer sits on the skin texture. You paint on them, then use your image as the skin. Paintbooth doesn't include them: download the complete packs once (needs internet), then everything works offline.
## 1. Download
- ETS2: [ETS2 truck & trailer templates](https://forum.scssoft.com/viewtopic.php?t=272386) (SCS forum)
- ATS: [ATS truck & trailer templates](https://forum.scssoft.com/viewtopic.php?t=288778) (SCS forum)

Open the first post of the topic and download the .zip from one of its links. The packs are large (the ATS pack is about 1 GB) and are updated for new game versions, so check the topic now and then.
## 2. Add to Paintbooth
1. **Settings > Templates > Import template pack (.zip)…** and pick the .zip you downloaded.
2. If the file name doesn't say which game it's for, Paintbooth asks.
3. It is unpacked into Documents\\Paintbooth\\Templates\\ETS2 (or ATS). This takes a minute; the status bar shows progress.

You can also unzip the pack into that folder yourself. Importing a newer pack later only copies what changed.
## 3. Use them
On an Advanced Skin's **Skins** tab, the *Templates* bar shows the folder for the chosen vehicle. **Open folder** opens it; **Choose folder…** fixes a wrong match (remembered).
> Lightbox, windscreen decal, door window, billboard and container templates are different: they come with Mods Studio 2. Use **Open template folder** at the top of those items.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Painting",
        "Images: sizes and formats",
        """
# Images: sizes and formats
## What Paintbooth accepts
- **PNG** (recommended), **TGA**, **JPG** and ready-made **DDS** files.
- Width and height must each be a **power of two**: 256, 512, 1024, 2048, 4096 or 8192. They don't have to be equal: 4096×2048 is fine.
- DDS files are used exactly as they are. Everything else is converted to DDS (DXT5 with mipmaps) when you export. Converted images are cached, so the next export is quick.

## Which size?
- Use the template's own size (typically 4096×4096 for cabins).
- 8192 gives more detail, but each 8192×8192 texture takes about 85 MB once converted, against about 21 MB at 4096×4096. That cost is in your mod's download size and the game's video memory.

## Transparency
How the alpha channel is used depends on the paint job settings (see *Paint job options*). For a plain full-colour skin, export without transparency.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Painting",
        "Accessories",
        """
# Accessories
Many truck and trailer parts (bumpers, mirrors, sun visors, side skirts, trailer chassis…) have their own textures. The **Accessories** tab tells the game which image goes on which parts.

1. Open the **Accessories** tab. Until you add an image, it lists every paintable accessory of the vehicle, grouped, with a search box.
2. Tick the parts one image should paint, then click **Add image for the ticked accessories** (or **Generate color** for a plain colour).
3. Choose the image (each accessory has its own template in the template pack).
4. Repeat for other parts. You can change the ticks of any image later in its own list.

## Generate color
**Generate color** makes a small 64×64 DDS of one colour, for parts that should simply be painted. Pick the colour (hex, RGB, SCS values, palettes or your saved colours) and the opacity. Below 100 % opacity, the colour lets the underlying paint show through.

## Where do accessory names come from?
From the vehicle's XML definition (see *Vehicles and XML files*). The game only paints accessories whose internal name (for example `f_bumper.shape3`) is listed. If a part doesn't change in game, its name is probably missing or misspelled in the XML.

> Right-click any list for Select all / none / invert, which act on what the search shows, or on one group.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Painting",
        "Paint job options (Main tab)",
        """
# Paint job options (Main tab)
## Basic
- **Skin Internal Name**, **Skin Name**, **Price**, **Unlock At Level**: see *Your first truck skin*.
- **Base Color**: the paint job's base colour. Players can change it in the shop unless **Base Color Locked** is ticked.
- **Alternate UV**: only for vehicles that have a second texture layout. Use it only when the template says so.

## Icon
The shop icon is 256×64. The default icon is used unless you choose your own image or **Use skin image**. **Use overlay** puts a PNG with transparency (for example your logo) on top.

## Metallic options
Turns on the metallic effect: **Flip Color** and **Flip Strength** tint the paint at grazing angles. The flake settings control the sparkle: **Flake Color**, **UV Scale** (flake size), **Density**, **Shininess** and **Clearcoat**.

## Mask options
A *mask* paint job lets players choose colours in game. The image's red, green and blue channels mark three areas; **Red/Green/Blue Color** are their starting colours, and the *Locked* boxes stop players changing them.

> Metallic and mask options exclude each other: when the mask is on, the metallic settings are not exported.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Vehicles",
        "Trailer skins",
        """
# Trailer skins
Owned trailers are painted like trucks, with two differences:
- Instead of cabins, the **Skins** tab lists the trailer's **bodies** (for example 13.7 m curtainsider, 15.2 m curtainsider). Tick the bodies each image fits.
- Paintbooth builds one paint job for the trailer and applies each image to its bodies. Chassis, axles, side skirts and bumpers go on the **Accessories** tab.

You need to own the trailer in game to choose a paint job for it.

> Trailer export follows the game's documented layout for owned trailers. Check your first trailer skin in game.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Vehicles",
        "Vehicles and XML files",
        """
# Vehicles and XML files
Paintbooth needs to know each vehicle's **cabins/bodies** and its **paintable accessories**. That comes from vehicle definition XML files in Documents\\Paintbooth\\Vehicles (**Home > Vehicles**).

## Getting vehicle files
- **Mods Studio 2 vehicle files** work as they are. Copy them into the Vehicles folder, or use **Import folder…** in the Vehicles window.
- **Scan game…** reads the game's own files and creates definitions for every SCS truck, including all cabins and the accessories SCS's paint jobs colour.
- **Save to library** (Vehicle tab) copies a vehicle stored inside an MS2 project into the library.
- Every MS2 project also carries the definitions of the vehicles it uses, so those show up even without library files.

## Several files for one vehicle
When two files use the same internal name (an ATS and an ETS2 version, or two authors' definitions), both are listed with the file name. Entries marked **no accessories** really have none. Pick the one that has them, or add the accessories yourself.

## Editing
Select a vehicle and edit its XML on the right. Changes are checked as you type, and **Save** reloads the library instantly; open projects offer to update.
- **+ Add accessory…**: name, internal name and group (type to search the existing groups). It is inserted next to the rest of its group.
- **+ Add cabin / body…**: the same for cabins and trailer bodies.
- **Ctrl+F** find and replace, **Ctrl+G** go to line, **Ctrl+/** comment, **Ctrl+D** duplicate a line.

## What the names mean
- A cabin's internal name (for example `l2h1_aero.volvo.fh_2024.cabin`) goes into the paint job's `suitable_for` list: the paint job is only offered for those cabins.
- An accessory's internal name (for example `mirror.shape3`) goes into the `acc_list` of the image that paints it.
- These names come from the game's definition files. After a game update renames parts, update the XML.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Vehicles",
        "Project files (.pb and .ms2)",
        """
# Project files (.pb and .ms2)
- Paintbooth projects are **.pb** files. A .pb is the same inside as a Mods Studio 2 **.ms2**: rename one to the other and either program opens it.
- Paintbooth also opens and saves **.ms2** files. Saving a project you didn't change gives a byte-identical file, and edits change only what you touched, so Mods Studio 2 can keep using the same file.
- New projects are saved into Documents\\Paintbooth\\Projects automatically as .pb, named after the mod. **Save as** (Ctrl+Shift+S) suggests a .pb; choose *Mods Studio 2 project* in the type list to keep .ms2. **Save** keeps the file's own type.
- Item types Paintbooth doesn't support stay in the file untouched and are skipped on export.
- Images are found even if the project moved, as long as they are somewhere in the project's folder.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Vehicles",
        "Lightbox, windscreen, billboard and more",
        """
# Lightbox, windscreen, billboard and more
**Home > Add more** adds items other than paint jobs:
- **Truck lightbox** (ETS2): your artwork in the Volvo FH3-FH6 roof lightbox. Pick the truck, internal name and name, the image, and whether it's painted.
- **Truck windscreen decal**: a windscreen decal. Some trucks offer accessory slot patterns (Jewel, In-Line, …) or windscreen types (Defrost): tick the ones you want and each becomes its own decal in the shop.
- **Truck door window** (ETS2): artwork on the door windows.
- For both, the truck list also has every other ETS2 truck, under **Paintbooth (from your game)**. Paintbooth ships their data, made from the game's own files, so nothing needs setting up. Their templates are in Documents\\Paintbooth\\Templates\\ETS2\\Windscreen decals and \\Door windows (**Open template folder** opens them). The windscreen template is the windscreen seen from the front; the door window template is the left door seen from outside, and the right door gets it mirrored so it reads the right way round on both. Door window art changes the windows of every truck that shares that window model.
- **Billboard**: replaces the roadside billboards of one model (up to the number shown). Only one billboard mod can be active in the game.
- **Cargo container V2**: a cargo carried in your own container skins (20 and 40 ft, 53 ft in ATS). Add a cargo by picking one of the game's cargos as a start, then give it your own internal name and change weight, value and companies.
- **Player company logo**: replaces the logos you can pick for your company (up to 8). Only one logo mod can be active.
- **Existing mod**: packs another mod (.scs or .zip) into this one. Your manifest replaces theirs. Drag the mod from Explorer onto the Paintbooth window: it goes into the open Existing mod item, or becomes a new one (drag several to add them all).
## Mods Studio 2 is needed
These items are made from Mods Studio 2's own template files (models, materials, PNG templates), so it must be installed on the same PC. Paintbooth finds it in C:\\Mods Studio 2 and the usual places; otherwise set the folder in **Settings > Mods Studio 2**.
> **Open template folder** (top right of the item) opens MS2's folder for the item, with the PNG templates to paint on.
> An empty shop icon uses the template's default icon.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Exporting",
        "Mod manifest",
        """
# Mod manifest
What the game's **Mod Manager** shows about your mod:
- **Mod Name**, **Mod Author**, **Mod Version**. Settings can add "by <creator>" and the version number to the end of the name ("My skins by AL3X V1.02"), and count the version up automatically (1.00, 1.01, …) on each export. The name the game will show appears under the field.
- **Icon**: the Mod Manager picture, 276×162 JPG. The default icon is used unless you choose one.
- **Description**: shown in the Mod Manager. New projects start with your default description (Settings); change it per project. If you leave it empty, the default is exported.
- **Mod Category**: for paint jobs, pick *paint_job*.
- **DLC Dependencies**: tick the DLCs the mod needs, for example a trailer or truck DLC.
- **Compatible Versions**: game versions such as `1.59.*` (one per line). Leave empty to skip the check.
- **Multiplayer Optional**: lets players without the mod join Convoy sessions with you.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Exporting",
        "Exporting and testing in game",
        """
# Exporting and testing in game
- **F5 / Export** writes `<project name>.scs` to the export folder for the game (Documents\\Paintbooth\\Exports\\ETS2 or ATS, changeable in Settings). No dialog: it overwrites the previous export.
- **File > Export…** asks where to save.
- **Shift+F5 / To mod folder** writes into the game's mod folder (Documents\\Euro Truck Simulator 2\\mod or Documents\\American Truck Simulator\\mod).
- **Show folder** opens the folder with the last export.

## In game
1. Start the game. On the profile screen, open the **Mod Manager**, enable the mod and confirm.
2. Load the profile and go where you can change your truck's (or owned trailer's) paint. Your paint job is listed under its skin name, with its icon.

> If the game is running with the mod loaded, Windows may not allow overwriting the file. Close the game, or export to the Exports folder and copy it later.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Help",
        "Troubleshooting",
        """
# Troubleshooting
## The paint job doesn't show up in game
- Is the mod enabled in the Mod Manager, and is it above mods that change the same vehicle?
- Were the right **cabins** ticked on the Skins tab? The paint job only appears for those cabins.
- Does **Compatible Versions** match your game version? Clear it to test.
- The game writes errors to **game.log.txt** in Documents\\Euro Truck Simulator 2 (or American Truck Simulator). Search it for your mod's name or "error".

## The texture looks wrong in game
- Wrong template for that cabin, or the image is for another cabin size: add a separate image per cabin layout.
- Is **Alternate UV** on when it shouldn't be (or the other way round)?
- Very dark or very bright paint: check Base Color and the mask/metallic options.

## Accessories aren't painted
- The part must be ticked for one of the accessory images.
- The accessory's internal name in the vehicle XML must match the game exactly. Game updates sometimes rename parts.

## Paintbooth problems
- Messages on the right of the status bar list everything Paintbooth checks; click them.
- Paintbooth's own error log: Documents\\Paintbooth\\paintbooth.log.
- If the item list has disappeared, press **Ctrl+M**, or drag the divider at the left edge.
- Windows SmartScreen warns about the installer because it isn't signed: **More info > Run anyway**.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Help",
        "Colours",
        """
# Colours
- **Select** next to any colour opens the colour picker: a colour square and hue bar; hex, RGB and **SCS** values; and **palettes**.
- **New palette…** makes your own palette. Pick a colour, then click **+ Add current colour**; right-click a colour in it to rename or remove it.
- **Save current** adds the colour to *Saved colours*. Saved colours, recent colours and your palettes are shared by all projects.

## Why SCS values look different
The game's .sii files store colours as **linear** values from 0 to 1, while image editors use sRGB values from 0 to 255. Mid-grey 128,128,128 is about 0.216 in linear. Paintbooth converts between the two for you; **Home > Color converter** does it by hand.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Help",
        "Keyboard shortcuts",
        """
# Keyboard shortcuts
## Projects
- **Ctrl+N** new project · **Ctrl+O** open · **Ctrl+S** save · **Ctrl+Shift+S** save as
- **F5** export · **Shift+F5** export to the game's mod folder
- **Ctrl+M** mod manifest · **Ctrl+D** duplicate the selected item · **Del** delete it
- **F1** tutorials · **Esc** closes the File side bar

## XML editor
- **Ctrl+F** find · **Ctrl+H** replace · **F3 / Shift+F3** next / previous
- **Ctrl+G** go to line · **Ctrl+/** comment / uncomment · **Ctrl+D** duplicate line
- **Tab / Shift+Tab** indent / outdent · **Ctrl+Z / Ctrl+Y** undo / redo · **Ctrl+plus / minus** text size
""",
    ),
    (
        "Help",
        "Folders and files",
        """
# Folders and files
Documents\\Paintbooth contains:
- **Projects**: your projects (.pb).
- **Vehicles**: vehicle XML files (Paintbooth or Mods Studio 2 format).
- **Templates\\ETS2**, **Templates\\ATS**: imported template packs.
- **Exports\\ETS2**, **Exports\\ATS**: exported mods.
- **Generated**: colour swatches made outside a project. Inside a project they go into its *generated* folder.
- **Defaults**: the default icons.
- **paintbooth.log**: the error log.

Paintbooth itself is installed in %LOCALAPPDATA%\\Programs\\Paintbooth (or the folder you chose in Setup). Uninstall it from Windows Settings > Apps. Your Documents\\Paintbooth folder is kept.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
    (
        "Help",
        "Glossary",
        """
# Glossary
- **.scs**: the game's mod file format. Paintbooth writes a ZIP-based .scs, which the game loads like any mod.
- **Paint job**: one entry in the game's paint shop. Each image in an Advanced Skin becomes one paint job (trucks) or one part of the paint job (trailers).
- **Template**: an image showing the texture layout of a vehicle or part.
- **UV map / UV layout**: how a 3D model's surface is unfolded onto a flat texture.
- **DDS**: the texture format the game uses. **DXT5** is its compression; **mipmaps** are smaller copies for distant views.
- **.tobj**: a small file that points the game at a .dds texture.
- **.sii**: the game's text definition files. A paint job is an `accessory_paint_job_data` unit.
- **suitable_for**: the cabins (or trailer bodies) a paint job is offered for.
- **acc_list**: the accessories an extra image paints.
- **Linear colour**: how .sii files store colours; see *Colours*.
- **Mods Studio 2 (MS2)**: another mod tool. Paintbooth reads and writes its project and vehicle files.
""",  # noqa: E501 - tutorial prose, one paragraph per source line
    ),
]


def _open_link(uri: str, _event: object = None) -> None:
    """Click handler for a link in the text (Tk passes the event last)."""
    open_web_url(uri)


class TutorialsWindow:
    """The tutorials window: searchable topic list on the left, the rendered topic on the right.

    Args:
        app: The main window (``App``); only its ``root`` and ``settings`` are used.
        topic: Title of the topic to show first.
    """

    def __init__(self, app: Any, topic: str | None = None):
        self.app = app
        w = self.win = tool_window(app, "tutorials", "Tutorials - Paintbooth", "1100x760")
        pane = ttk.Panedwindow(w, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, padding=(10, 10, 4, 10))
        right = ttk.Frame(pane, padding=(4, 10, 10, 10))
        pane.add(left, weight=1)
        pane.add(right, weight=4)
        self.q = tk.StringVar()
        sr = ttk.Frame(left)
        sr.pack(fill="x", pady=(0, 6))
        ttk.Label(sr, image=icons().get("search", 14)).pack(side="left", padx=(0, 4))
        e = ttk.Entry(sr, textvariable=self.q)
        e.pack(side="left", fill="x", expand=True)
        Tooltip(e, "Search all tutorials")
        self.q.trace_add("write", lambda *a: self._render_list())
        self.tv = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tv.pack(fill="both", expand=True)
        self.tv.tag_configure("section", font="SkBold")
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show())
        self._links = 0  # counter for the per-link tags of the topic being written
        base = tkfont.nametofont("TkDefaultFont").actual()
        size = base["size"] if base["size"] > 0 else 10  # negative sizes are pixels; use 10 pt
        self._build_text(right, base["family"], size)
        self._render_list(select=topic)

    def _build_text(self, right: ttk.Frame, fam: str, size: int) -> None:
        """The read-only text widget, its scroll bar and the markup's text tags."""
        self.text = tk.Text(
            right,
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=24,
            pady=16,
            background=C["field"],
            foreground=C["fg"],
            insertbackground=C["fg"],
            selectbackground=C["select"],
            font=(fam, size + 2),
            spacing1=2,
            spacing3=4,
            cursor="arrow",
        )
        sb = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        t = self.text
        t.tag_configure("h1", font=(fam, size + 9, "bold"), spacing1=4, spacing3=12)
        t.tag_configure("h2", font=(fam, size + 4, "bold"), spacing1=14, spacing3=6, foreground=C["accent"])
        t.tag_configure("bold", font=(fam, size + 2, "bold"))
        t.tag_configure("italic", font=(fam, size + 2, "italic"))
        t.tag_configure(
            "code",
            font=(tkfont.nametofont("TkFixedFont").actual()["family"], size + 1),
            background=C["raised"],
        )
        t.tag_configure("bullet", lmargin1=18, lmargin2=36)
        t.tag_configure("step", lmargin1=18, lmargin2=40)
        t.tag_configure(
            "tip",
            lmargin1=18,
            lmargin2=18,
            rmargin=18,
            background=C["raised"],
            spacing1=8,
            spacing3=8,
            foreground=C["fg"],
        )
        t.tag_configure("hit", background=C.get("match", "#5a4a12"))
        t.tag_configure("link", foreground="#6aa6ff" if C["bg"] < "#808080" else C["accent"], underline=True)
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="arrow"))

    @staticmethod
    def _matches(title: str, body: str, q: str) -> bool:
        """True if the (lower-case) search ``q`` is empty or in the topic's title or text."""
        return not q or q in title.lower() or q in body.lower()

    def _render_list(self, select: str | None = None) -> None:
        """Redraw the topic list for the search, and show ``select`` (a title) or keep the selection."""
        q = self.q.get().strip().lower()
        cur = select or (self.tv.selection()[0] if self.tv.selection() else None)
        self.tv.delete(*self.tv.get_children())
        sections: dict[str, str] = {}
        first = None
        for i, (sec, title, body) in enumerate(TOPICS):
            if not self._matches(title, body, q):
                continue
            if sec not in sections:
                sections[sec] = self.tv.insert("", "end", text=sec, open=True, tags=("section",))
            iid = f"t{i}"
            self.tv.insert(sections[sec], "end", iid=iid, text="  " + title)
            first = first or iid
            if select and title == select:
                cur = iid
        target = cur if cur and self.tv.exists(cur) else first
        if target:
            self.tv.selection_set(target)
            self.tv.see(target)
            self._show()
        else:
            self._write("# No results\nNothing matches your search.")

    def _show(self) -> None:
        """Render the selected topic and highlight the search text in it."""
        sel = self.tv.selection()
        if not sel or not sel[0].startswith("t"):
            return
        _, _, body = TOPICS[int(sel[0][1:])]
        self._write(body)
        q = self.q.get().strip()
        if q:
            start = "1.0"
            first = None
            while True:
                pos = self.text.search(q, start, stopindex="end", nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(q)}c"
                self.text.tag_add("hit", pos, end)
                first = first or pos
                start = end
            if first:
                self.text.see(first)

    def _inline(self, line: str, base_tags: tuple[str, ...] = ()) -> None:
        """Insert one line, turning links, **bold**, *italic* and `code` into tagged runs."""
        for part in re.split(r"(\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)", line):
            m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
            if m:
                self._link(m.group(1), m.group(2), base_tags)
                continue
            if part.startswith("**") and part.endswith("**"):
                self.text.insert("end", part[2:-2], (*base_tags, "bold"))
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                self.text.insert("end", part[1:-1], (*base_tags, "italic"))
            elif part.startswith("`") and part.endswith("`"):
                self.text.insert("end", part[1:-1], (*base_tags, "code"))
            elif part:
                self.text.insert("end", part, base_tags)

    def _link(self, text: str, uri: str, base_tags: tuple[str, ...]) -> None:
        """Insert a ``[text](uri)`` link; only http(s) links are clickable (plain text otherwise)."""
        if not is_web_url(uri):
            self.text.insert("end", text, base_tags)
            return
        tag = f"link{self._links}"  # one tag per link, so each click knows its own URL
        self._links += 1
        self.text.insert("end", text, (*base_tags, "link", tag))
        self.text.tag_bind(tag, "<Button-1>", functools.partial(_open_link, uri))

    def _write(self, body: str) -> None:
        """Render one topic's markup into the text widget (see the markup note on TOPICS)."""
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._links = 0
        for raw in body.strip("\n").split("\n"):
            line = raw.rstrip()
            if line.startswith("# "):
                t.insert("end", line[2:] + "\n", ("h1",))
            elif line.startswith("## "):
                t.insert("end", line[3:] + "\n", ("h2",))
            elif line.startswith("- "):
                t.insert("end", "•  ", ("bullet",))
                self._inline(line[2:], ("bullet",))
                t.insert("end", "\n", ("bullet",))
            elif re.match(r"^\d+\. ", line):
                n, rest = line.split(" ", 1)
                t.insert("end", f"{n}  ", ("step", "bold"))
                self._inline(rest, ("step",))
                t.insert("end", "\n", ("step",))
            elif line.startswith("> "):
                self._inline(line[2:], ("tip",))
                t.insert("end", "\n", ("tip",))
            elif not line:
                t.insert("end", "\n")
            else:
                self._inline(line)
                t.insert("end", "\n")
        t.configure(state="disabled")
        t.yview_moveto(0)


def open_tutorials(app: Any, topic: str | None = None) -> TutorialsWindow:
    """Show the tutorials window (reusing the open one), optionally at ``topic``.

    The window is kept as ``app._tutorials``; a closed one is detected by the TclError on use.
    """
    w = getattr(app, "_tutorials", None)
    if w is not None:
        try:
            w.win.deiconify()
            w.win.lift()
            if topic:
                w._render_list(select=topic)
            return w
        except tk.TclError:
            pass  # the old window was closed: make a new one
    app._tutorials = TutorialsWindow(app, topic)
    return app._tutorials
