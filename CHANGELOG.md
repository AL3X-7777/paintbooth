# Changelog

Each release on GitHub shows the section for its version. Add a new section at the top before
tagging a release (see RELEASING.md).

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
