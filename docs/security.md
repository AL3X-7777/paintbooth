# Security model

Paintbooth runs on the user's own PC with the user's rights, and it opens files made by other
people: shared projects, template packs from forums, other people's mods. This page lists those
inputs and how each one is checked. If you add a feature that reads a file or opens a link, add it
here.

## What is trusted

* The user, and what they type or pick in a dialog.
* Paintbooth's own files and the user's settings.
* The user's Mods Studio 2 installation folder (the templates it contains are run as build actions).

Everything else is untrusted, including the *contents* of a project file.

## Untrusted inputs and their checks

| Input | Risk | Check | Code | Test |
|---|---|---|---|---|
| Template pack (ZIP or folder) | A member named `..\..\Startup\x.bat`, an absolute path, a drive or UNC path writes outside `Templates\` | Every member goes through `safepath.safe_join`; unsafe ones are skipped and logged. Symlinks in folders are skipped. | `templates.import_pack` | `test_core_safety.TemplatePackZipSlip` |
| Project file (`.pb`/`.ms2`): build actions | A shared project carries its own build actions. `CopyFile From=..\..\secret.txt` would pack a local file into the mod; `WriteTextFile ..\..\x` would write outside the export folder. | `Engine.norm` refuses paths that leave the mod; `ms2data.ci_path` refuses paths that leave the template folder (including through symlinks); `Builder.build_files` checks every output path again. | `ms2tpl.py`, `ms2data.py`, `builder.py` | `EnginePaths`, `CiPath`, `BuildOutputPaths` |
| Project file: regular expressions | A pattern that takes forever to match and freezes the window, or one that is not valid | Patterns are limited to 1000 characters, and a repeated group inside a repeat such as `(a+)+` is refused. This is a heuristic for the well-known shape, not a time limit: Python's `re` has none. An invalid or refused pattern becomes a validation problem, not a crash. | `ms2tpl.compile_dotnet` | `DotnetRegex` |
| Project file: text values | Control characters that make the saved file unreadable | `xmldom` drops characters XML 1.0 forbids; `Ms2Project.save` re-parses before replacing the file. | `xmldom.py`, `ms2.py` | `XmlDom`, `test_save_with_control_characters_reopens` |
| Project and template files: links | `webbrowser.open` hands a UNC path or `file:` link to Windows, which may run it | Only absolute `http`/`https` links are clickable. | `gui/tpleditor.is_web_url` | `LinkSchemeTest` |
| Another mod's archive ("Existing mod" item) | Member names with `..`; a small archive that unpacks to a huge size | Unsafe names are skipped; 1 GB per member and 4 GB per archive. | `ms2tpl.read_archive`, `hashfs.py` | `ArchiveMembers`, `HashFsReading` |
| Model files (`.pmd`) | Offsets pointing outside the file | Every offset is bounds-checked; damage raises `PmdError`. | `pmd.py` | `PmdErrors` |
| Game archives (HashFS) | Damaged tables, lying sizes | Bounds checks and capped decompression; damage raises `HashFsError`. | `hashfs.py` | `HashFsHandles` |
| Local browser UI (`paintbooth ui`) | Any web page in the user's browser can send requests to `127.0.0.1` (DNS rebinding, cross-site requests) | Listens on `127.0.0.1` only. The `Host` header must be the server's own, a foreign `Origin` is refused, and every `/api/` call needs a random per-session token. Bodies are limited to 4 MB. | `server.py` | `test_cli_server.ServerApi` |
| Update check | A changed answer from the network | HTTPS to `api.github.com` only, 5 s timeout. The version is only compared, and the link shown must start with the project's own releases address, else the plain releases page is used. Nothing is downloaded or run. | `updates.py` | `test_project_io.Updates` |
| Installer payload | A changed installer writing outside the install folder | Entry names with `..`, drives or absolute paths are refused. Switches are parsed as whole tokens. | `installer/setup.c` | `test_installer.SetupHarnessTest` |

## The one rule

**Text from a file never becomes a path without going through `paintbooth/safepath.py`.**

* `clean_member(name)` returns a safe relative path or raises `UnsafePathError`. It rejects `..`,
  absolute and UNC paths, any part containing `:` (a drive letter or an NTFS data stream), parts
  ending in a dot or space, and Windows device names such as `NUL`.
* `safe_join(root, name)` does that and then checks the resolved result is still inside `root`, which
  also catches a symbolic link that points elsewhere.

## What Paintbooth does not do

* It does not run code from projects, templates or mods. The build actions are a fixed list
  (see [build actions](reference/build-actions.md)); an unknown action is an error.
* It does not send any data anywhere. The update check is one plain request to GitHub with
  nothing about the user in it, and it can be turned off in Settings. The privacy statement is in
  [CODE_SIGNING.md](../CODE_SIGNING.md).
* It does not need admin rights. The installer writes to the user's own folders and registry branch.

## Build and supply chain

* GitHub Actions are pinned to full commit hashes; the workflow has read-only rights except the
  release job.
* Every Python package in CI and in the installer is pinned with its SHA-256 hash
  (`requirements-dev.txt`, `wheels/requirements-offline.txt`); pip refuses anything else.
* Releases can be signed through SignPath; see [CODE_SIGNING.md](../CODE_SIGNING.md).

## Reporting a problem

Open an issue at <https://github.com/AL3X-7777/paintbooth/issues>. For something that could harm
users before a fix is out, use GitHub's private "Report a vulnerability" on the repository's
Security tab.
