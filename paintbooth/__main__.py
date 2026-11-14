"""Command line entry point: ``paintbooth <command> ...`` (or ``python -m paintbooth``).

Commands:

* ``app [project]`` - the desktop window (the default when no command is given);
* ``export project.pb|.ms2 [-o out.scs]`` - export a GUI project to a mod;
* ``new``, ``check``, ``build``, ``watch``, ``ui`` - the ``project.xml`` pipeline
  (:mod:`paintbooth.model`, :mod:`paintbooth.builder`, :mod:`paintbooth.session`,
  :mod:`paintbooth.server`);
* ``scan`` - write vehicle XML from extracted game files or a mod.

Commands exit with status 0 on success and 1 (or an error message) on failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import xml.parsers.expat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .model import Project

log = logging.getLogger(__name__)


def cmd_new(a: argparse.Namespace) -> None:
    """Create a ``project.xml`` project folder."""
    from .newproject import create_project

    d = Path(a.dir)
    try:
        create_project(d, a.package, a.title, a.author, a.game)
    except FileExistsError as e:
        sys.exit(str(e))
    print(
        f"Created project in {d}.\nNext: put vehicle XML in {d / 'vehicles'} "
        f"(or run 'paintbooth scan <extracted game folder> -o {d / 'vehicles'}'),\n"
        f"add skins to project.xml, then 'paintbooth app {d}'."
    )


def _load(dir_: str) -> Project:
    """Load a ``project.xml`` project and print its problems."""
    from .model import load_project

    p = load_project(Path(dir_))
    for i in p.issues:
        print(i)
    return p


def cmd_check(a: argparse.Namespace) -> None:
    """Validate a ``project.xml`` project without building; exit 1 on errors."""
    from .builder import plan

    p = _load(a.dir)
    if p.errors:
        sys.exit(1)
    _, issues = plan(p)
    for i in issues:
        print(i)
    if any(i.level == "error" for i in issues):
        sys.exit(1)
    print(f"OK: {len(p.skins)} skin(s), {len(p.vehicles)} vehicle(s)")


def cmd_build(a: argparse.Namespace) -> None:
    """Build a ``project.xml`` project; exit 1 if the build failed."""
    from .builder import Builder

    p = _load(a.dir)
    b = Builder(None if a.no_cache else p.root / ".paintbooth" / "cache", workers=a.jobs)
    try:
        r = b.build(p, Path(a.output) if a.output else None)
    finally:
        b.close()
    for i in r.issues:
        if i not in p.issues:
            print(i)
    sys.exit(0 if r.ok else 1)


def cmd_watch(a: argparse.Namespace) -> None:
    """Build, then rebuild whenever a project file changes, until Ctrl+C."""
    from .session import Session

    s = Session(Path(a.dir), auto_build=True, log_to_console=True)
    for i in s.project.issues:
        print("   ", i)
    if not s.project.errors:
        s.build()
    s.start()
    print("Watching for changes - edit project.xml, vehicle XML or images. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass  # Ctrl+C is how watch mode ends
    finally:
        s.stop()


def cmd_app(a: argparse.Namespace) -> None:
    """Open the desktop window."""
    from .app import main as app_main

    app_main([a.dir] if a.dir else [])


def cmd_ui(a: argparse.Namespace) -> None:
    """Run the local browser UI."""
    from .server import serve

    try:
        serve(Path(a.dir), port=a.port, open_browser=not a.no_browser, auto_build=a.auto_build)
    except OSError as e:
        sys.exit(str(e))


def load_settings() -> dict[str, Any]:
    """Return the GUI's saved settings (e.g. the Mods Studio 2 folder), or ``{}`` if there are none."""
    from .appdirs import settings_file

    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable settings: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _configure_templates(settings: Mapping[str, Any]) -> None:
    """Give the template builder the user's settings (it reads ``ms2_dir``)."""
    from . import tplbuild

    tplbuild.configure(dict(settings))


def cmd_export(a: argparse.Namespace) -> None:
    """Export a .pb/.ms2 project to a .scs mod; exit 1 on validation errors or a failed build."""
    from .appdirs import cache_dir
    from .builder import Builder
    from .ms2 import Ms2Project
    from .ms2build import plan_ms2

    _configure_templates(load_settings())
    try:
        p = Ms2Project.load(Path(a.project))
        files, probs = plan_ms2(p)
    except (OSError, ValueError, RuntimeError, xml.parsers.expat.ExpatError) as e:
        sys.exit(f"{a.project}: {e}")
    for x in probs:
        where = "manifest" if x.item < 0 else f"item {x.item + 1}"
        print(f"{x.level.upper()}: {where}: {x.message}")
    if any(x.level == "error" for x in probs):
        sys.exit(1)
    out = Path(a.output) if a.output else Path(a.project).with_suffix(".scs")
    b = Builder(cache_dir())
    try:
        r = b.build_files(files, out, "fast", out.parent)
    finally:
        b.close()
    sys.exit(0 if r.ok else 1)


def cmd_scan(a: argparse.Namespace) -> None:
    """Write vehicle XML from a folder or ZIP mod."""
    from .scanner import HashFsArchiveError, scan

    try:
        written = scan(Path(a.source), Path(a.output), game=a.game or "", overwrite=a.overwrite)
    except (HashFsArchiveError, OSError) as e:
        sys.exit(str(e))
    print(f"Wrote {len(written)} vehicle file(s) to {a.output}")


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    ap = argparse.ArgumentParser(
        prog="paintbooth", description=f"Paintbooth {__version__} - fast offline ETS2/ATS skin mod builder"
    )
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("app", help="open the Paintbooth window (default when no command is given)")
    s.add_argument("dir", nargs="?", help="a project to open (.pb or .ms2)")
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("new", help="create a new project folder")
    s.add_argument("dir")
    s.add_argument("--package")
    s.add_argument("--title")
    s.add_argument("--author")
    s.add_argument("--game", choices=["ets2", "ats"], default="ets2")
    s.set_defaults(fn=cmd_new)

    for name, fn, hlp in (
        ("check", cmd_check, "validate the project without building"),
        ("build", cmd_build, "build the .scs mod"),
        ("watch", cmd_watch, "rebuild automatically whenever a file changes"),
        ("ui", cmd_ui, "browser version of the UI (local only)"),
    ):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("dir", nargs="?", default=".")
        s.set_defaults(fn=fn)
        if name == "build":
            s.add_argument("-o", "--output", help="override the output path (.scs/.zip or folder)")
            s.add_argument("-j", "--jobs", type=int, help="CPU cores to use (default: all)")
            s.add_argument("--no-cache", action="store_true", help="re-encode every texture")
        if name == "ui":
            s.add_argument("--port", type=int, default=8765)
            s.add_argument("--no-browser", action="store_true")
            s.add_argument("--auto-build", action="store_true")

    s = sub.add_parser("export", help="export a project (.pb or Mods Studio 2 .ms2) to a .scs mod")
    s.add_argument("project")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("scan", help="make vehicle XML from extracted game defs or a mod .scs (ZIP)")
    s.add_argument("source", help="folder containing def/vehicle/..., or a ZIP .scs mod")
    s.add_argument("-o", "--output", default="vehicles", help="folder to write vehicle XML into")
    s.add_argument("--game", choices=["ets2", "ats"])
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(fn=cmd_scan)
    return ap


def main(argv: Sequence[str] | None = None) -> None:
    """Parse ``argv`` (default: the process arguments) and run the command.

    Raises:
        SystemExit: With the command's exit status.
    """
    ap = _parser()
    a = ap.parse_args(argv)
    if a.cmd is None:
        a = ap.parse_args(["app"])
    a.fn(a)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # the Windows build spawns encoder processes from this executable
    main()
