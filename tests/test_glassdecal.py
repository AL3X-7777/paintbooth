"""Windscreen decals and door windows for any truck (paintbooth.glassdecal), on a synthetic truck.

The truck has a cabin with a windscreen (glass facing -z), a small extra bit of glass that must
be ignored, a ``set_glass`` locator, and an opening windows model with a left and right window.
No SCS game files are needed.
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from paintbooth import glassdecal as gd, pmd, pmg
from paintbooth.xmldom import E, parse
from test_coaches import empty_pmg
from test_core_engine import make_pmd

GLASS = (
    'effect : "eut2.glass.rfx" {\n\tdiffuse : { 0.04 , 0.04 , 0.04 }\n'
    '\ttexture : "texture_base" {\n\t\tsource : "/g.tobj"\n\t}\n}\n'
)


def front_quad(z: float, x0: float, x1: float, y0: float, y1: float) -> pmg.Piece:
    pos = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], "f4")
    nrm = np.tile(np.array([0, 0, -1], "f4"), (4, 1))
    return pmg.Piece(0, pos, nrm, [np.zeros((4, 2), "f4")], None, np.array([[0, 2, 1], [0, 3, 2]]))


def side_quad(x: float) -> pmg.Piece:
    pos = np.array([[x, 2, -2], [x, 2, -1], [x, 3, -1], [x, 3, -2]], "f4")
    nrm = np.tile(np.array([np.sign(x), 0, 0], "f4"), (4, 1))
    return pmg.Piece(0, pos, nrm, [np.zeros((4, 2), "f4")], None, np.array([[0, 1, 2], [0, 2, 3]]))


def truck_files() -> dict[str, bytes]:
    windscreen = front_quad(-2.5, -1.1, 1.1, 2.0, 3.0)
    stray = front_quad(-2.5, 3.0, 3.2, 3.5, 3.6)  # far away: not part of the windscreen
    cab = pmg.read(pmg.add_pieces(pmg.read(empty_pmg()), [windscreen, stray]))
    cab_g = pmg.add_locator(cab, "set_glass")
    win_g = pmg.add_pieces(pmg.read(empty_pmg()), [side_quad(-1.2), side_quad(1.2)])
    cabin_def = (
        b'SiiNunit\n{\naccessory_cabin_data : c1.acme.road.cabin\n{\n\tmodel: "/v/cab.pmd"\n'
        b'\twindows_model: "/v/win.pmd"\n}\n}\n'
    )
    return {
        "v/cab.pmd": make_pmd([["/m/glass.mat"]]),
        "v/cab.pmg": cab_g,
        "v/win.pmd": make_pmd([["/m/glass.mat"]]),
        "v/win.pmg": win_g,
        "m/glass.mat": GLASS.encode(),
        "def/vehicle/truck/acme.road/cabin/c1.sii": cabin_def,
    }


def mock_bundle(path: Path):
    from unittest import mock

    return mock.patch.object(gd, "BUNDLED", path)


class Reader:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def __call__(self, path: str) -> bytes | None:
        return self.files.get(path)

    def listdir(self, path: str) -> list[str]:
        return sorted(k.rsplit("/", 1)[1] for k in self.files if k.rsplit("/", 1)[0] == path)


class GlassDecalTest(unittest.TestCase):
    def setUp(self):
        self.read = Reader(truck_files())

    def test_cabins_and_windscreen(self):
        (cab,) = gd.truck_cabins(self.read, "acme.road")
        self.assertEqual((cab.id, cab.model, cab.windows), ("c1.acme.road.cabin", "/v/cab.pmd", "/v/win.pmd"))
        glass = gd.windscreen(gd.load_cabin(self.read, cab.model))
        self.assertEqual(len(glass.triangles), 2)  # the stray bit is left out
        hookup, ext, inner = gd.windscreen_models(gd.load_cabin(self.read, cab.model), "/c", "/o", "/i")
        self.assertEqual(hookup, "set_glass")
        self.assertEqual(pmd.material_table(ext[0]), [["/c", "/o"]])
        out = pmg.read(ext[1]).pieces
        self.assertEqual(len(out), 2)  # alpha-tested core + blended layer
        self.assertLess(float(out[0].positions[:, 2].max()), -2.5)  # lifted forwards, off the glass
        uv = out[0].uvs[0]
        self.assertTrue(((uv > 0) & (uv < 1)).all())
        # seen from the front the truck's right (+x) is on the image's left
        right = out[0].positions[:, 0] > 0
        self.assertLess(uv[right, 0].max(), uv[~right, 0].min() + 1e-6)
        self.assertEqual(len(pmg.read(inner[1]).pieces), 1)

    def test_door_window_model_keeps_skinning_and_mirrors_sides(self):
        win = gd.load_cabin(self.read, "/v/win.pmd")
        d, g = gd.door_window_model(win, {"/m/glass.mat": "/new/glass.mat"})
        self.assertEqual(pmd.material_table(d), [["/new/glass.mat"]])
        m = pmg.read(g)
        self.assertEqual(len(m.data), len(win.model.data))  # only UVs changed, in place
        left, right = m.pieces
        # the front (-z) is on the image's left on the left door, on its right on the right door
        lf = left.uvs[0][left.positions[:, 2] < -1.5, 0].mean()
        lb = left.uvs[0][left.positions[:, 2] > -1.5, 0].mean()
        rf = right.uvs[0][right.positions[:, 2] < -1.5, 0].mean()
        rb = right.uvs[0][right.positions[:, 2] > -1.5, 0].mean()
        self.assertLess(lf, lb)
        self.assertGreater(rf, rb)
        text = gd.glass_material(GLASS, "/art.tobj")
        self.assertIn('source : "/art.tobj"', text)
        self.assertIn("diffuse : { 1.000000", text)  # not the glass's near-black
        self.assertIn("/x.tobj", gd.glass_material('effect : "eut2.glass.rfx" {\n}\n', "/x.tobj"))

    def test_plans_and_templates(self):
        with tempfile.TemporaryDirectory() as t:
            img = Path(t) / "art.png"
            Image.new("RGBA", (64, 32), (255, 0, 0, 255)).save(img)
            files = gd.plan_windscreen(
                gd.GameSource(self.read), "acme.road", "deco", "Deco", "10", "0", img, img
            )
            paths = {f.path for f in files}
            self.assertIn("def/vehicle/truck/acme.road/accessory/set_glass/deco.sii", paths)
            sii = next(f for f in files if f.path.endswith("deco.sii")).data.decode()
            self.assertIn('suitable_for[]: "c1.acme.road.cabin"', sii)
            self.assertIn("deco.acme.road.set_glass", sii)
            door = {f.path for f in gd.plan_door_window(gd.GameSource(self.read), "acme.road", img)}
            self.assertTrue({"v/win.pmd", "v/win.pmg", "v/pb_win_art.dds"} <= door)
            written = gd.write_templates(gd.GameSource(self.read), [("acme.road", "Acme Road")], Path(t))
            self.assertEqual(
                sorted(p.relative_to(t).as_posix() for p in written),
                ["Door windows/Acme Road.png", "Windscreen decals/Acme Road.png"],
            )
        with self.assertRaises(ValueError):
            gd.plan_door_window(gd.GameSource(self.read), "nobody.here", Path("x.png"))

    def test_bundle_gives_the_same_files_as_the_game(self):
        """A bundle made from the game files plans the same mod files, with no game folder."""
        import json
        import zipfile

        game = gd.GameSource(self.read)
        (cab,) = game.cabins("acme.road")
        hookup, ext, inner = game.windscreen(cab.model)
        win = game.windows(cab.windows)
        with tempfile.TemporaryDirectory() as t:
            zp = Path(t) / "glass.zip"
            index = {
                "trucks": {"acme.road": {"name": "Acme Road", "cabins": [vars(cab)]}},
                "windscreens": {cab.model: {"hookup": hookup, "ext": "ws_0_ext.pmg", "int": "ws_0_int.pmg"}},
                "windows": {
                    cab.windows: {
                        "pmd": "win_0.pmd",
                        "pmg": "win_0.pmg",
                        "glass": win.glass,
                        "mats": {"/m/glass.mat": "mat_0_0.mat"},
                    }
                },
            }
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("index.json", json.dumps(index))
                z.writestr("ws_0_ext.pmg", ext)
                z.writestr("ws_0_int.pmg", inner)
                z.writestr("win_0.pmd", win.pmd)
                z.writestr("win_0.pmg", win.pmg)
                z.writestr("mat_0_0.mat", win.mats["/m/glass.mat"])
            bundle = gd.BundleSource(zp)
            self.assertEqual(bundle.trucks(), {"acme.road": "Acme Road"})
            img = Path(t) / "art.png"
            Image.new("RGBA", (64, 32), (255, 0, 0, 255)).save(img)

            def data(files):
                return {f.path: f.data for f in files if f.data is not None}

            for plan in (
                lambda src: gd.plan_door_window(src, "acme.road", img),
                lambda src: gd.plan_windscreen(src, "acme.road", "deco", "Deco", "10", "0", img, None),
            ):
                self.assertEqual(data(plan(bundle)), data(plan(game)))
            self.assertEqual(len(gd.write_templates(bundle, [("acme.road", "Acme Road")], Path(t))), 2)
            self.assertIsNone(bundle.windscreen("/nope.pmd"))
            self.assertIsNone(bundle.windows("/nope.pmd"))
            self.assertEqual(bundle.cabins("nobody"), [])
        with mock_bundle(Path("/no/such.zip")):
            self.assertIsNone(gd.bundled())

    def test_truck_list_options(self):
        el = parse(
            "<Item><Variables><Variable><Name>Truck</Name><Value>daf_xd</Value><PredefinedValues>"
            "<anyType><ChildVariables><Variable><Name>InternalNames</Name><Value><string>daf.xd</string>"
            "</Value></Variable></ChildVariables><Value>daf_xd</Value></anyType>"
            "</PredefinedValues></Variable></Variables></Item>"
        )
        added = gd.add_native_trucks(el, [("daf.xd", "DAF XD"), ("acme.road", "Acme Road")])
        self.assertEqual(added, 1)  # DAF XD is already offered by the template
        self.assertEqual(gd.add_native_trucks(el, [("acme.road", "Acme Road")]), 0)  # no duplicates
        value = gd.native_value("acme.road")
        self.assertTrue(gd.is_native(value))
        self.assertEqual(gd.native_vehicle(el, value), "acme.road")
        self.assertIsNone(gd.native_vehicle(el, "daf_xd"))
        self.assertEqual(gd.add_native_trucks(E("Item", E("Variables")), [("a.b", "A")]), 0)


if __name__ == "__main__":
    unittest.main()


class RibbonLayoutTest(unittest.TestCase):
    """The ribbon layout helpers need no display."""

    def test_clean_and_move(self):
        try:
            from paintbooth.gui import ribbon as rb
        except ImportError:  # Python without Tk
            self.skipTest("needs tkinter")
        self.assertEqual(rb.clean_layout(None), rb.DEFAULT_LAYOUT)
        self.assertEqual(rb.clean_layout("junk"), rb.DEFAULT_LAYOUT)
        lay = rb.clean_layout([{"caption": "A", "items": ["new", "nope", "new", "add:Billboard"]}, "x"])
        self.assertEqual(lay, [{"caption": "A", "items": ["new", "add:Billboard"]}])
        lay = [{"caption": "A", "items": ["a", "b", "c"]}, {"caption": "B", "items": ["d"]}]
        rb.move_item(lay, "a", 0, 3)  # to the end of its own group
        self.assertEqual(lay[0]["items"], ["b", "c", "a"])
        rb.move_item(lay, "d", 0, 0)
        self.assertEqual((lay[0]["items"], lay[1]["items"]), (["d", "b", "c", "a"], []))
        rb.move_item(lay, "new", 1, None)  # a new button
        self.assertEqual(lay[1]["items"], ["new"])
        self.assertIsNotNone(rb.button_for("add:TruckLightbox"))
        self.assertIsNone(rb.button_for("unknown"))
