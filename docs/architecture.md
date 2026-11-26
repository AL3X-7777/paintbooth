# Architecture

Paintbooth is a Python 3.11+ desktop program with a tkinter window. Its third-party libraries are
Pillow and numpy, plus the optional `tkinterdnd2` (drag and drop of files; bundled by the installer). It never needs the internet; the one optional network call is the update check. The
Python package is called `paintbooth`.

## Layers

```
┌──────────────────────────────── paintbooth.gui ────────────────────────────────┐
│ main.App ── ribbon · startscreen · settings · tutorials · filedrop             │
│   ├─ project_ctl.ProjectController   open / save / close / watch the file      │
│   ├─ export_ctl.ExportController     snapshot → background export → Notice     │
│   ├─ library_ctl.LibraryController   the user's vehicle library                │
│   └─ editors (manifest, Advanced Skin) · tpleditor (MS2 template items) · tools │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ only calls the core; the core never imports the GUI
┌───────────────▼────────────────── core (paintbooth.*) ───────────────────────────┐
│ Project model   ms2 (+ xmldom, ms2skeleton)            .pb / .ms2 files        │
│ Validation      ms2build.validate · tplbuild.validate_item · names              │
│ Planning        ms2build.plan_ms2 → list[OutFile]                               │
│                   └ tplbuild.plan_item → ms2tpl.Engine (+ pmd, ms2data)         │
│ Building        builder.Builder.build_files → bcn (DXT) · formats · packer      │
│ Game data       hashfs · scanner · vehlib · library · templates                 │
│ Models          pmd · pmg · coach (coach paint) · glassdecal (glass items)      │
│ Support         appdirs · safepath · util · updates                             │
└─────────────────────────────────────────────────────────────────────────────────┘
```

The GUI depends on the core and never the other way round. Everything below the line works without a
display and is what the command line (`paintbooth export project.pb`) and the tests use.

## The export pipeline

Exporting goes through three stages that are kept strictly apart, so each can be tested alone.

1. **Validate:** `ms2build.validate(project)` returns `Problem`s, errors and warnings, each tied
   to an item and a field. The editors call it while you type, debounced to 300 ms. It never raises
   on bad user input: NaN colours, unknown vehicles and broken regexes all come back as problems.
2. **Plan:** `ms2build.plan_ms2(project)` turns the project into a list of `builder.OutFile`s. Each
   one holds either ready bytes (a `.sii`, `.mat` or `.tobj`) or a `TextureJob` saying "encode this
   image as DXT5 at this size". Nothing is written yet.
   * *Advanced skins* are planned directly in `ms2build`.
   * *Coaches* get their paint fitted from the blueprint and their window and trim films planned by
     `coach`.
   * *Windscreen decals and door windows for trucks without an MS2 template* are planned by
     `glassdecal`, from the data Paintbooth ships in `data/glass.zip` (or the game folder).
   * *Mods Studio 2 template items*, such as a lightbox, cargo or windscreen decal, are planned by
     `tplbuild.plan_item`. It runs the item's build actions in `ms2tpl.Engine`, a small interpreter
     for MS2's `template.xml` language; see [build actions](reference/build-actions.md). The engine
     reads the template files from the user's own Mods Studio 2 folder, never from Paintbooth.
3. **Build:** `builder.Builder.build_files(files, out)` encodes the texture jobs in parallel
   worker processes, with a cache keyed by source file, size and settings. It then writes a `.scs`
   with `packer`, or a plain folder. Every mod path is checked with `safepath` before it is written.

The output is deterministic: the same project gives byte-identical files. A Mods Studio 2 lightbox
project exported by Paintbooth matches Mods Studio 2's own export file for file.

## Threads and processes

* **The Tk thread** owns all widgets and the open `Ms2Project`. Nothing else touches either.
* **Export.** `ExportController` serialises a *snapshot* of the project on the Tk thread and plans and
  builds from that copy in a worker thread. So you can keep editing while an export runs. The result
  is passed back with `root.after`.
* **Texture encoding** runs in a `ProcessPoolExecutor`. Each worker sets `bcn.THREADS = 1`, so N
  processes use N cores, not N×8 threads. If the pool can't start, breaks, or no texture finishes
  for two minutes (`Builder.stall_seconds`), it falls back to threads, so an export never hangs.
  While it runs, the status bar shows the current step and the seconds taken, and each step is
  logged to `paintbooth.log`.
* **Background helpers:** the update check, the thumbnail decoder and the template-pack import are
  daemon threads that report back through `root.after`.

## Where state lives

| State | Owner | Notes |
|---|---|---|
| Settings | `gui/prefs.py`, file `~/.paintbooth.json` | Saved atomically, with a `.bak` copy; a damaged file is kept as `.broken` |
| Settings the core needs (`ms2_dir`) | `tplbuild.configure(settings)` | Set once at start-up by the GUI and the CLI |
| User folders | `appdirs` | `Documents\Paintbooth\…`; `PAINTBOOTH_HOME` moves all of them, which the tests use |
| Texture cache | `builder.Builder` | `~/.paintbooth/cache`, pruned to 2 GB, oldest first |
| Small caches | module-level `lru_cache`s | All bounded: MS2 data lists, template index, thumbnails (64), folder index (8 folders) |
| Log | `paintbooth.log` | Rotating, 1 MB × 3 |

## Two project formats

* **`.pb` / `.ms2`:** the main format, and what the window edits. `.pb` is Paintbooth's name for the
  same XML that Mods Studio 2 writes as `.ms2`, so files go both ways unchanged (`ms2.py`, `xmldom.py`).
* **`project.xml` folders:** the older format behind `paintbooth new/check/build/watch/ui`, with a small
  browser UI (`model`, `builder.plan`, `session`, `server`). It is kept for scripted and batch use and
  shares the builder, formats and packer with the main format.

## Design rules

* The core never imports tkinter, and the GUI never builds mod files itself.
* Untrusted text never becomes a path without `safepath`; see [security](security.md).
* Writing a file the user relies on goes through a temp file and `os.replace`, so a crash never
  leaves half a file.
* No `compatible_versions` is written to `manifest.sii` unless the user typed one.
* Golden-output tests guard the export bytes. Change them only on purpose.
