# File formats

Every file Paintbooth reads or writes, where the code for it lives, and the details that are easy
to get wrong. The module docstrings hold the exact byte layouts; this page is the map.

## Project files: `.pb` and `.ms2`

| | |
|---|---|
| Code | `paintbooth/ms2.py` (model), `paintbooth/xmldom.py` (XML), `paintbooth/ms2skeleton.py` (new projects) |
| Direction | read and write |

A `.pb` file is the same XML that Mods Studio 2 saves as `.ms2`; only the extension differs. The
root is `<Project xsi:type="ScsProject">` with a `<ModTemplates>` list. Each `<Template>` is one
item of the mod: the manifest, an Advanced Truck Skin, or a Mods Studio 2 template item such as a
lightbox. An item carries its values in `<Variables>` and, for template items, its own copy of the
`<BuildActions>` that produce its files.

The files are written exactly as .NET's `XmlSerializer` writes them: two-space indentation, CRLF line
ends, `<Tag />` for empty elements, an `encoding="utf-16"` declaration on UTF-8 bytes, and no final
newline. `xmldom` keeps every element it doesn't understand, so opening a Mods Studio 2 project and
saving it changes only what the user edited. A test compares the round trip byte for byte.

`Ms2Project.save` writes to a temporary file, parses the result to prove it opens again, and only then
replaces the project file.

## Mods Studio 2 templates: `template.xml`

| | |
|---|---|
| Code | `paintbooth/ms2data.py` (finding and listing), `paintbooth/ms2tpl.py` (running), `paintbooth/tplbuild.py` (validation and planning) |
| Direction | read only, from the user's own Mods Studio 2 folder (Settings > Mods Studio 2 folder) |

`Data\ModProjects\<GAME>\Templates\<Name>\template.xml` describes one kind of item: its variables,
their validators, and the build actions. The other files in that folder (models, `.sii` snippets,
icons) are what the actions copy and change. Paintbooth ships none of them. See
[build actions](reference/build-actions.md) for the action language.

Things to know:

* File names in templates use `\`, mixed case and sometimes a doubled separator. `ms2data.ci_path`
  looks them up without regard to case, as Windows would, and refuses anything that leaves the folder.
* `WriteTextFile` and `AppendTextFile` end every line with CRLF, including the last.
* Regular expressions are .NET's. `ms2tpl.dotnet_regex` translates named groups and replacement
  syntax (`$1`, `${name}`) to Python's.

## Model descriptors: `.pmd` (version 4)

| | |
|---|---|
| Code | `paintbooth/pmd.py` |
| Direction | read, and rewrite of the material table |

A 64-byte header of sixteen `uint32` values, then the sections it points at. For each *look* of the
model there is one material path per material slot. Paintbooth changes those paths so a copied
template model uses the mod's own textures, and can drop looks. The material strings are the last
block of the file, so the table is rebuilt in full: one string per entry, as the game's own tools
write it. Automatic material names are the CityHash64 of the material text (`/automat/xx/<hash>.mat`).

## Texture objects: `.tobj`

| | |
|---|---|
| Code | `paintbooth/formats.py` (`tobj_bytes`, `read_tobj_path`), `paintbooth/gui/tools.py` (TOBJ editor) |
| Direction | write; the editor also reads |

Magic `0x70B10A01`, a 40-byte header, then the texture path with a length prefix. Bytes 20 to 39 are
the sampler settings. Paintbooth has three known sets ("skin", "ui", "game"), taken from mods that
load in the game. The TOBJ editor keeps header settings it doesn't recognise.

## Textures: `.dds`

| | |
|---|---|
| Code | `paintbooth/bcn.py` (DXT1/DXT5 encoder), `paintbooth/formats.py` (containers) |
| Direction | write |

Skins are DXT5 (or DXT1 without alpha) with a full mip chain in a classic DDS container. Shop icons
are uncompressed BGRA in a DX10 container marked sRGB. The encoder is written with numpy and tuned for
smooth semi-transparent edges. Texture sides must be powers of two (`util.is_pot`); validation says so
before the game shows a black texture.

## Game text files: `.sii`, `.sui`, `.mat`

| | |
|---|---|
| Code | `paintbooth/formats.py`, `paintbooth/ms2build.py` |
| Direction | write |

Plain text units. Names inside them are SCS *tokens*: at most 12 characters from `a-z 0-9 _`
(`paintbooth/names.py` holds the one rule). A skin's internal name is limited to 10, because the game
adds a `_0` suffix. `manifest.sii` gets `compatible_versions[]` only when the user typed a version.

## Mod archives: `.scs`

| | |
|---|---|
| Code | `paintbooth/packer.py` (write, ZIP), `paintbooth/hashfs.py` (read, HashFS v1 and v2) |
| Direction | write ZIP; read both |

Paintbooth writes mods as ZIP archives named `.scs`, which the game accepts. Already-compressed
textures are stored, text is deflated. The game's own archives and some mods use *HashFS* instead:
files are found by the CityHash64 of their path, so the archive holds no names. `hashfs.py` reads
plain files and directory listings from both versions. That is enough for the vehicle scanner
(`paintbooth/scanner.py`), and for repacking an existing mod. Decompression is size-capped.

## Vehicle library: `Vehicles\*.xml`

| | |
|---|---|
| Code | `paintbooth/vehlib.py` |
| Direction | read and write |

One XML file per vehicle: its id, cabins (variants) and paintable accessories. Two dialects are read:
Paintbooth's own, and the vehicle element used in Mods Studio 2 projects. The scanner can generate
these files from the game's `def` data.

## Template packs

| | |
|---|---|
| Code | `paintbooth/templates.py` |
| Direction | read (import from ZIP or folder) |

Community paint job template packs (PSD and PNG files in one folder per vehicle). Import extracts
them into `Templates\<GAME>`; every member name goes through `safepath`. Many ZIP tools store UTF-8
names without setting the UTF-8 flag, so names are re-decoded when that gives valid text.

## Settings: `.paintbooth.json`

| | |
|---|---|
| Code | `paintbooth/gui/prefs.py`, `paintbooth/appdirs.py` |
| Direction | read and write |

One JSON object in the user's home folder. Written atomically with a `.bak` of the previous file.
Paths saved under the program's earlier names are mapped to `Documents\Paintbooth` when read.

## Folder projects: `project.xml`

| | |
|---|---|
| Code | `paintbooth/model.py`, `paintbooth/newproject.py` |
| Direction | read; `paintbooth new` writes a starter |

The older, hand-editable format for the command line: a `project.xml` plus `vehicles/*.xml` and an
art folder. See `paintbooth new --help`.

## Model geometry: `.pmg` (version 0x15)

| | |
|---|---|
| Code | `paintbooth/pmg.py` (read, add pieces and locators, new models), `paintbooth/coach.py` (coach templates and films) |
| Direction | read; write new models; add a locator |

The meshes of a model, in "pieces" that each use one material slot of the matching `.pmd`. Vertices
are interleaved (position, normal, colour, tangent, UV sets); indices are 16 bit. Paintbooth reads
the coaches' bodies to draw UV templates, and writes new one-part models holding the *films* for
painted windows and trim (`pmg.new_model`, with `pmd.new_model` for the descriptor). Accessory
models are drawn around a locator of the cabin named after their type (`f_bumper`...), so
Paintbooth moves them onto it (`pmg.locators`). The films are an accessory of type `pb_film`; the
cabin's `.pmg` is written back with one extra locator of that name at its origin
(`pmg.add_locator`) and is otherwise copied byte for byte.

The film accessory (`def/vehicle/bus/<coach>/accessory/pb_film/<paint job>.sii`) is suitable only
for its own paint job, and the paint job lists it in `overrides[]`, so choosing the paint job
puts the films on and other paint jobs leave the coach without them. Its `exterior_model` has the
window films (opaque where painted) and the trim films facing out; its `interior_model` has the
window films facing in at half opacity. The materials blend (`...decal.over`) instead of alpha
testing, so soft edges in the artwork stay soft.

## Coach paint masks and blueprints

The coaches' paint UV space is a virtual 4096x4096 texture made of two 4096x2048 tiles, named
after their place: `pjm_at_0x0_size_4096x2048` (upper half) and `pjm_at_0x2048_size_4096x2048`
(lower half). Which half a body paints is read from its model's UVs (`coach.paint_region`), not
from the names SCS's own paint jobs use, because the game loads every tile of a paint job's folder.

Users paint coaches on a *blueprint* (`coach.blueprint_template`): flat views from the left, right,
front, rear and top. `coach.bake` turns it into the paint mask by tracing every texel of the
game's UV layout back to its point on the coach and from there to the blueprint; window and trim
films use blueprint positions as their UVs, so all three line up.
