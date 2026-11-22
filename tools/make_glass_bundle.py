"""Build ``paintbooth/data/glass.zip``: windscreen decal and door window data for every ETS2 truck.

Paintbooth ships this file so windscreen decals and door windows work for every SCS truck without
the game folder (see :mod:`paintbooth.glassdecal`). It is made from the game's own files::

    python tools/make_glass_bundle.py "F:\\SteamLibrary\\steamapps\\common\\Euro Truck Simulator 2" glass.zip

It only uses functions that Paintbooth 0.20.0 already has, so it also runs with the Python and
the Paintbooth of an installed copy (``tools/make_glass_bundle.bat`` does that on Windows).

Contents (a flat zip, all names lower case):

* ``index.json``: ``{"version", "game_version", "trucks": {vehicle id: {"name", "cabins":
  [{"id", "model", "windows"}]}}, "windscreens": {cabin model: {"hookup", "ext", "int"}},
  "windows": {windows model: {"pmd", "pmg", "glass": [piece index...], "mats": {material
  path: file}}}}``;
* ``ws_<n>_ext.pmg`` / ``ws_<n>_int.pmg``: the windscreen decal models (materials 0, 1 outside;
  0 inside), already in the hookup locator's space, with template UVs;
* ``win_<n>.pmd`` / ``win_<n>.pmg``: an opening windows model, with side-view UVs on its glass;
* ``mat_<n>.mat``: the windows' original glass materials.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import zipfile
from pathlib import Path

try:
    import paintbooth  # noqa: F401 - an installed copy (PYTHONPATH), or this repository
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build(game: Path, out: Path, log=print) -> dict:  # noqa: ANN001 - also runs on older Paintbooth
    """Write the bundle for the game at ``game`` into ``out``; return its index."""
    from paintbooth import glassdecal as gd
    from paintbooth import pmd
    from paintbooth.coach import load_cabin
    from paintbooth.library import BUNDLED
    from paintbooth.scanner import game_reader
    from paintbooth.vehlib import load_library

    read = game_reader(game)
    lib, _ = load_library(BUNDLED)
    trucks = sorted(
        (v.id, f"{v.brand} {v.name}".strip())
        for v in lib.values()
        if v.game == "ets2" and not v.is_trailer and not v.is_bus
    )
    version = (read("version.txt") or b"").decode("utf-8", "replace").strip()
    index: dict = {
        "version": datetime.date.today().strftime("%Y.%m.%d"),
        "game_version": version,
        "trucks": {},
        "windscreens": {},
        "windows": {},
    }
    files: dict[str, bytes] = {}
    for vid, name in trucks:
        cabins = gd.truck_cabins(read, vid)
        if not cabins:
            log(f"{vid}: no cabins found")
            continue
        index["trucks"][vid] = {
            "name": name,
            "cabins": [{"id": c.id, "model": c.model, "windows": c.windows} for c in cabins],
        }
        for c in cabins:
            if c.model not in index["windscreens"]:
                index["windscreens"][c.model] = None
                try:
                    made = gd.windscreen_models(load_cabin(read, c.model), "/0", "/1", "/2")
                except ValueError as e:
                    log(f"{vid}: {c.model}: {e}")
                    made = None
                if made is not None:
                    n = len([w for w in index["windscreens"].values() if w])
                    hookup, ext, inner = made
                    files[f"ws_{n}_ext.pmg"], files[f"ws_{n}_int.pmg"] = ext[1], inner[1]
                    index["windscreens"][c.model] = {
                        "hookup": hookup,
                        "ext": f"ws_{n}_ext.pmg",
                        "int": f"ws_{n}_int.pmg",
                    }
            if c.windows and c.windows not in index["windows"]:
                index["windows"][c.windows] = None
                try:
                    win = load_cabin(read, c.windows)
                    made = gd.door_window_model(win, {})
                except ValueError as e:
                    log(f"{vid}: {c.windows}: {e}")
                    made = None
                if made is not None:
                    n = len([w for w in index["windows"].values() if w])
                    files[f"win_{n}.pmd"], files[f"win_{n}.pmg"] = win.pmd_data, made[1]
                    mats = {}
                    for path in pmd.material_table(win.pmd_data)[0]:
                        text = read(path.lstrip("/"))
                        if text and b"glass" in text.split(b"{", 1)[0] and path not in mats:
                            mats[path] = f"mat_{n}_{len(mats)}.mat"
                            files[mats[path]] = text
                    glass = [
                        i for i, p in enumerate(win.model.pieces) if "glass" in win.mats[p.material].effect
                    ]
                    index["windows"][c.windows] = {
                        "pmd": f"win_{n}.pmd",
                        "pmg": f"win_{n}.pmg",
                        "glass": glass,
                        "mats": mats,
                    }
        log(f"{vid}: {len(cabins)} cabins")
    index["windscreens"] = {k: v for k, v in index["windscreens"].items() if v}
    index["windows"] = {k: v for k, v in index["windows"].items() if v}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.json", json.dumps(index, indent=1, sort_keys=True))
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    log(
        f"wrote {out}: {len(index['trucks'])} trucks, {len(index['windscreens'])} windscreens, "
        f"{len(index['windows'])} window models"
    )
    return index


def main(argv: list[str] | None = None) -> int:
    """Command line: ``make_glass_bundle.py GAME_FOLDER OUT.zip``."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("game", type=Path, help="the ETS2 game folder")
    ap.add_argument("out", type=Path, help="the zip to write")
    a = ap.parse_args(argv)
    if not (a.game / "def.scs").is_file():
        print(f"{a.game} is not an ETS2 game folder (no def.scs)")
        return 1
    build(a.game, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
