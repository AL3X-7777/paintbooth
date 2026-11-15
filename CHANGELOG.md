# Changelog

Each release on GitHub shows the section for its version. Add a new section at the top before
tagging a release (see RELEASING.md).

## 0.17.0
- **Coaches: windows and trim per body.** In the Skins tab every coach body now has a second
  line, "<body> - windows and trim"; tick it to paint that body's windows and gloss-black trim
  with the image. (This replaces the "Paint windows and gloss-black trim" tick box; in older
  projects tick the new lines again.)
- Window films are now fully opaque from the outside where the artwork is, and trim films show
  (they were missing, and windows were too see-through). The films no longer take the dark
  shading of the glass.
- **Export to mod folder** button in the Project group of the Home tab.
- The "exported" message has an **OK** button (any key or click still closes it).

## 0.16.0
- **Coach window and trim films reworked:**
  - They now come with the paint job: they are a separate accessory that only the Paintbooth
    paint job puts on, so other paint jobs no longer show them. The coach's own model is no
    longer replaced (it only gets an invisible hook point for the films).
  - Windows are fully covered from the outside where the artwork is opaque, and let half the
    light through from the inside.
  - Soft edges in the artwork stay soft on the films, like on the body (they were cut off hard).
  - Bumpers, panels and other parts are now placed where they really are on the coach; before,
    they were drawn in the wrong place on the blueprint, so their paint and trim films did not
    line up. **The coach blueprints change**: the new ones are written at start-up; please move
    your artwork onto them.
  - Each paint job gets its own films (with several skins for one coach, all showed the first).

## 0.15.0
- **Vehicle library:** every ETS2 truck, coach and ownable trailer (87 vehicles, game 1.62),
  with the names you see in the game's shop: cabins, trailer bodies and the parts that take the
  paint job. Built by Paintbooth from the game's own files (no Mods Studio 2 data).
  - It is installed automatically the first time Paintbooth starts, without internet.
  - **Vehicles > Install vehicle library** installs it again: the newest version from GitHub
    when online, else the built-in one, plus the truck, coach and trailer mods in your ETS2
    mod folder.
  - It goes to `Documents\Paintbooth\Vehicles\Library`; your own vehicle files are never
    touched.
- **Scan game folder** now also finds ownable trailers and uses the in-game names.

## 0.14.0
- Coach blueprints now include the bumpers, panels and other parts the MAN and Scania get from
  the vehicle's own list (only the Volvo's were found before). The templates are rewritten at
  the next start.
- **Windows and black trim are painted by default** on coaches (untick *Paint windows and
  gloss-black trim* to keep them stock). Leave a window or trim area transparent in your image to
  keep it as it is; any colour there, white included, goes onto the glass at half strength and
  onto the trim fully.
- The MAN's gloss-black parts (made with a black texture) are now recognised as trim; chrome is
  no longer mistaken for it. Gloss-black accessories such as the front grille get the paint too.
- Coach blueprint images can be any size (they no longer need power-of-two sides).

## 0.13.0
- **Coaches appear by themselves:** Paintbooth finds ETS2 in your Steam libraries and adds the
  three coaches to the vehicle list at start-up (no scan needed), with their templates in
  `Templates\ETS2\Buses`.
- **One template per coach body, the blueprint:** flat views of the coach from every side with
  the body, bumpers and other accessories, windows and black trim drawn in. Paint it like a
  drawing of the coach; Paintbooth fits it onto the game's own layout when exporting, so a
  design runs seamlessly across the paint, the accessories and (with the option) the windows.
  The game's layout is still there as `... UV layout.png` for those who prefer it (tick
  *Images are drawn on the UV layout*).
- Painted windows and trim now use the same image as the paint; there's no second image any more.
- Fixed: the taskbar showed Python's icon instead of Paintbooth's.

## 0.12.0
- **Coaches (ETS2 Coaches DLC):** the Volvo 9700 Double Decker, Scania Touring and MAN Lion's
  Coach can be skinned like trucks. Run **Vehicles > Scan game folder** once: it adds the coaches
  to your vehicle list and writes their templates to `Templates\ETS2\Buses`.
- Each coach body has two templates: `... paint.png` (the paint job, 4096x2048) and
  `... windows and trim.png` (flat views of the coach from every side).
- **Paint windows and gloss-black trim** (coaches only, off by default): paint the second
  template and tick the option. Windows show the artwork half see-through, also from inside;
  transparent pixels keep the stock glass and trim. This replaces the coach's model while the mod
  is active, so all paint jobs of that body show it, and it needs a new export after a game update
  that changes the coach.

## 0.11.0
- **New name and logo:** Paint booth is now **Paintbooth**. Installing over an older version moves
  the program, the shortcuts and `Documents\Paint booth` to the new name; projects and settings come
  along.
- **Security:** a template pack can no longer write files outside the Templates folder, and a
  project file from someone else can no longer pull your files into a mod or write outside the
  export folder. Links inside project files open only if they are web links.
- **Fixed:**
  - Opening Settings could fail with an error.
  - A project with unusual characters pasted into a field could no longer be opened.
  - Two vehicles whose names differ only by `.` and `_` overwrote each other's skin without a warning.
  - New folder projects no longer write `compatible_versions` unless you type one.
  - Saving an image while an export was running could stop the export.
  - The TOBJ editor kept only the settings it knew; unknown ones are now preserved.
  - With automatic versioning, an export no longer saves your other unsaved edits.
  - Automatic versioning reset versions such as `1.5` or `2.10.1` to `1.00`; it now keeps your
    scheme and raises the last number.
  - A failed "Save as" no longer redirects the next save to the new file name.
  - A full or read-only cache folder no longer makes an export fail.
- Settings are saved safely (a backup is kept, and a damaged file is set aside, not reset).
- The texture cache is limited to 2 GB, and the log file to 4 MB.
- Editing stays responsive in big project folders: file lookups are cached and wait until you stop typing.
- For developers: the Python package is now `paintbooth`; 225 automated tests; full documentation in
  `docs/`.

## 0.10.0
- The SCS Tools tab is gone: Color converter, TOBJ editor and Vehicles are on the Home tab,
  between Export and Program.
- A message tells you where the mod was exported (click the folder to open it; any key or click
  closes it). It can be turned off in Settings.
- Installer: choose the install folder, optional desktop icon, publisher details. Updates go to
  the folder of the previous install.
- Paintbooth tells you when a newer version is on GitHub (only when online; Settings can turn it off).
- Links to the ETS2 and ATS template packs, and a "Getting template packs" tutorial.

## 0.9.0
- Version number at the end of the mod name (optional), default mod description.
- Projects are saved as .pb (Mods Studio 2 .ms2 still opens and saves).
- New items made with your Mods Studio 2 templates: truck lightbox, windscreen decal, door window,
  billboard, cargo container V2, player company logo, existing mod.

## 0.8.0
- New DXT5/DXT1 encoder: smooth semi-transparent edges.
- Renamed from Skinyard to Paint booth.
