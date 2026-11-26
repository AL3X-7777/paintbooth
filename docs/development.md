# Development

How to work on Paintbooth: set-up, tests, the quality gates, the documentation and releases.

## Set-up

You need Python 3.11 or newer with tkinter (on Windows: the python.org installer with "tcl/tk and
IDLE" ticked).

```
git clone https://github.com/AL3X-7777/paintbooth
cd paintbooth
pip install -e ".[dev]"
python -m paintbooth.app            # start the window from source
python -m paintbooth --help         # the command line
```

Offline on Windows, `install.bat` installs Pillow and numpy from the `wheels` folder of a source zip
(the wheels are not in git; each release's source zip carries them).

## Layout

| Path | Contents |
|---|---|
| `paintbooth/` | The core: project model, validation, export, file formats ([module list](reference/modules.md)) |
| `paintbooth/gui/` | The tkinter window |
| `paintbooth/data/` | The logo, the list of vehicle display names, the built-in vehicle library (`library/`) and the glass data (`glass.zip`) |
| `installer/` | `setup.c` (Setup.exe), `launcher.c` (Paintbooth.exe), `winsetup.py` (shortcuts, registry), `build_installer.py` |
| `tests/` | Unit and GUI tests |
| `tools/gen_docs.py` | Documentation generator |
| `tools/make_glass_bundle.py`, `.bat` | Builds `paintbooth/data/glass.zip` from the game |
| `packs/` | The vehicle library and community vehicle packs Paintbooth downloads |
| `docs/` | This documentation |
| `.github/workflows/release.yml` | Lint, test, build, and release a new version on GitHub |
| `.github/workflows/past-releases.yml` | Builds and publishes the earlier versions listed in `.github/past-releases.txt` |

## Tests

```
python -m unittest discover -s tests
```

* Every test file starts with `import _isolation`. It points `HOME` and `PAINTBOOTH_HOME` at a
  temporary folder, so tests never touch your real `Documents\Paintbooth` or settings.
* GUI tests (`test_gui_smoke.py`, `test_gui_editors.py`) build the real window. They skip when there
  is no display. On Linux run them with `xvfb-run -a python -m unittest discover -s tests`.
* Coverage of the core must stay at 80 % or more (it is about 93 %):
  `python -m coverage run -m unittest discover -s tests && python -m coverage report`.

| Test file | Covers |
|---|---|
| `test_core_safety.py` | Path safety: template packs, build-action paths, archives, output paths |
| `test_core_engine.py` | Template engine actions, PMD, .NET regex translation, builder, caches, HashFS |
| `test_ms2.py`, `test_ms2tpl.py` | Project round trips byte for byte, skins and template items end to end |
| `test_project_io.py` | XML edge cases, names, new projects, folders and migration, scanner, vehicle library, update check |
| `test_cli_server.py` | Every command-line command, the local browser UI and its security checks |
| `test_folder_projects.py`, `test_templates.py`, `test_vehicles.py` | `project.xml` builds, template matching, vehicle definitions |
| `test_installer.py` | Installer logic: registry values, shortcuts, moving and removing installs, release notes, pinned requirements |
| `test_gui_smoke.py`, `test_gui_editors.py` | The main window and every editor and tool window |

### The export must not change by accident

The same project must give the same bytes. `test_ms2.py` and `test_ms2tpl.py` pin this for the built-in
samples. If you change `formats.py`, `bcn.py`, `ms2build.py` or `ms2tpl.py`, export a project you
know before and after and compare the `.scs` contents; a Mods Studio 2 export of the same project is
the reference for template items.

## Quality gates

CI runs these on every push; run them before committing. All must be clean.

```
ruff check .
ruff format --check .
mypy
python tools/gen_docs.py --check --no-api
```

The rules are in `pyproject.toml`. In short:

* **ruff** with pycodestyle, pyflakes, bugbear, pylint, pydocstyle (Google style), bandit and more.
  Complexity is limited (15 branches, 60 statements per function).
* **mypy** checks everything; the format and model modules must be fully annotated.
* A `# noqa` or `# type: ignore` needs the rule code and a reason on the same line.

## Code conventions

* Every module, class and function has a docstring. Say what it is for and what a caller must know;
  put *why* comments next to anything a reader would trip over (byte layouts, Mods Studio 2 quirks).
* The core never imports tkinter. The GUI never builds mod files itself.
* Catch specific exceptions. A handler that must catch everything (a worker thread, a Tk callback)
  logs with `log.exception`.
* Text from a file never becomes a path without `safepath` ([security](security.md)).
* Files the user relies on are written to a temporary file and moved into place.
* User-facing text says "Paintbooth", one word.

## Documentation

`python tools/gen_docs.py` regenerates `docs/reference/` and the HTML API reference in `docs/api/`
(needs `pip install pdoc`). CI fails if `docs/reference/` is stale. The other pages are hand-written;
update them when behaviour changes.

## Releasing

See [RELEASING.md](../RELEASING.md). In short: raise `__version__` in `paintbooth/__init__.py` (the
only place), add a section to `CHANGELOG.md`, commit and push to `main`. When the checks pass and the
version has no release yet, GitHub tags the commit, builds the installer, the portable zip, the source
zip and the documentation zip, and publishes the release. Versions from before that are rebuilt from
their own commits by `past-releases.yml` (list: `.github/past-releases.txt`).

## Vehicle packs

`packs/` holds the two packs *Install vehicle library* downloads (from `raw.githubusercontent.com`,
branch `main`): `vehicle-library.zip` (a zip of `paintbooth/data/library`; rebuild it whenever that
folder changes, and raise the version in its `index.json`) and `community-vehicles.zip` (community
XMLs, flat, with an `index.json`). Each zip is flat: XML files plus `index.json`.

## Glass data

`paintbooth/data/glass.zip` (see [file formats](file-formats.md)) is made from the game, not by
hand. After a game update that adds or changes trucks, rebuild it:

```
python tools/make_glass_bundle.py "F:\SteamLibrary\steamapps\common\Euro Truck Simulator 2" paintbooth/data/glass.zip
```

On a PC with only an installed Paintbooth, `tools/make_glass_bundle.bat` does the same with the
installed Python and writes `glass.zip` next to itself.

## Building the installer yourself

On Windows with Python 3.13:

```
pip install --require-hashes -r requirements-dev.txt
python installer/build_installer.py --python "C:\Path\To\Python313" --out dist
```

`build_installer.py` copies that Python as the bundled runtime, compiles `setup.c` and `launcher.c`
with zig (from the `ziglang` package), packs everything into a ZIP and appends it to Setup.exe. The
layout of the result is described at the top of `installer/setup.c`.
