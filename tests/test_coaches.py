"""Coaches DLC support: .pmg geometry, paint regions, UV templates, window/trim films, export.

Everything runs on a small synthetic coach (a box with a paint panel, a window and a black trim
strip), so no SCS game files are needed or shipped.
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from paintbooth import coach, pmd, pmg, tplbuild, vehlib
from paintbooth.builder import Builder
from paintbooth.ms2 import Ms2Project
from paintbooth.ms2build import plan_ms2, validate
from test_core_engine import make_pmd

PAINT_MAT = 'effect : "eut2.truckpaint.tsnmapuv.rfx" {\n\tdiffuse : { 1 , 1 , 1 }\n\tshininess : 250\n}\n'
GLASS_MAT = (
    'effect : "eut2.glass.rfx" {\n\tdiffuse : { 1 , 1 , 1 }\n\tglass_tint_color : { 0.6 , 0.6 , 0.6 }\n}\n'
)
LAMP_GLASS = 'effect : "eut2.glass.rfx" {\n\tglass_tint_color : { 1.0 , 0.1 , 0.0 }\n}\n'
TRIM_MAT = (
    'effect : "eut2.dif.spec.weight.add.env.tsnmap.rfx" {\n'
    "\tdiffuse : { 0.006 , 0.006 , 0.006 }\n\tshininess : 250\n}\n"
)
MATS = ["/m/paint.mat", "/m/glass.mat", "/m/lamp.mat", "/m/trim.mat"]
TEXTS = [PAINT_MAT, GLASS_MAT, LAMP_GLASS, TRIM_MAT]


def empty_pmg() -> bytes:
    """A valid 0x15 .pmg with one part and no pieces."""
    head = struct.Struct("<4sIIIiIQ4f6f10I")
    part = struct.pack("<QIIII", 1, 0, 0, 0, 0)
    off = head.size
    end = off + len(part)  # locators, pieces, strings and pools are all empty and start here
    return (
        head.pack(b"\x15gmP", 0, 1, 0, 0, 0, 0, *[0.0] * 10, off, off, end, end, end, 0, end, 0, end, 0)
        + part
    )


def quad(material: int, x: float, uv: np.ndarray, normal: tuple[float, float, float]) -> pmg.Piece:
    """A 2-triangle quad on the side x = ``x`` (z from -5 to 5, y from 1 to 3)."""
    pos = np.array([[x, 1, -5], [x, 1, 5], [x, 3, 5], [x, 3, -5]], "f4")
    nrm = np.tile(np.array(normal, "f4"), (4, 1))
    return pmg.Piece(material, pos, nrm, [uv.astype("f4")], None, np.array([[0, 1, 2], [0, 2, 3]]))


def build_coach() -> tuple[bytes, bytes, dict[str, bytes]]:
    """Return (.pmd, .pmg, {material path: text}) of the synthetic coach."""
    top_half = np.array([[0.1, 0.1], [0.9, 0.1], [0.9, 0.4], [0.1, 0.4]])
    pieces = [
        quad(0, -1.2, top_half, (-1, 0, 0)),  # paint, left side
        quad(1, -1.25, np.zeros((4, 2)), (-1, 0, 0)),  # window
        quad(2, 1.2, np.zeros((4, 2)), (1, 0, 0)),  # tail lamp glass: left alone
        quad(3, 1.25, np.zeros((4, 2)), (1, 0, 0)),  # gloss-black trim
    ]
    model = pmg.read(empty_pmg())
    g = pmg.add_pieces(model, pieces)
    files = {m.lstrip("/"): t.encode() for m, t in zip(MATS, TEXTS, strict=True)}
    return make_pmd([MATS]), g, files


class PmgFormat(unittest.TestCase):
    def test_round_trip_and_errors(self):
        _, g, _ = build_coach()
        m = pmg.read(g)
        self.assertEqual([p.material for p in m.pieces], [0, 1, 2, 3])
        self.assertEqual(pmg.add_pieces(m, []), g)
        extra = pmg.add_pieces(m, [m.pieces[1]])
        m2 = pmg.read(extra)
        self.assertEqual(len(m2.pieces), 5)
        for a, b in zip(m.pieces, m2.pieces, strict=False):  # old pieces unchanged after the move
            np.testing.assert_array_equal(a.positions, b.positions)
            np.testing.assert_array_equal(a.uvs[0], b.uvs[0])
        with self.assertRaises(pmg.PmgError):
            pmg.read(b"\x14gmP" + g[4:])
        with self.assertRaises(pmg.PmgError):
            pmg.read(g[:200])
        big = pmg.Piece(
            0,
            np.zeros((70000, 3), "f4"),
            np.zeros((70000, 3), "f4"),
            [np.zeros((70000, 2), "f4")],
            None,
            np.zeros((0, 3), int),
        )
        with self.assertRaises(pmg.PmgError):
            pmg.add_pieces(m, [big])

    def test_pmd_add_materials(self):
        d = make_pmd([["/a.mat", "/b.mat"], ["/c.mat", "/d.mat"]])
        out = pmd.add_materials(d, ["/x.mat"])
        self.assertEqual(
            pmd.material_table(out), [["/a.mat", "/b.mat", "/x.mat"], ["/c.mat", "/d.mat", "/x.mat"]]
        )
        self.assertEqual(pmd.look_names(out), pmd.look_names(d))


class CoachModel(unittest.TestCase):
    def setUp(self):
        d, g, mats = build_coach()
        self.files = {"vehicle/bus/test/cab.pmd": d, "vehicle/bus/test/cab.pmg": g, **mats}
        self.cab = coach.load_cabin(self.files.get, "/vehicle/bus/test/cab.pmd")

    def test_classification(self):
        self.assertEqual([coach.is_paint(m) for m in self.cab.mats], [True, False, False, False])
        self.assertEqual([coach.is_window(m) for m in self.cab.mats], [False, True, False, False])
        self.assertEqual([coach.is_gloss_black(m) for m in self.cab.mats], [False, False, False, True])

    def test_region_and_templates(self):
        region = coach.paint_region(self.cab)
        self.assertEqual(region, coach.TILES[0])
        self.assertEqual(region.name, "pjm_at_0x0_size_4096x2048")
        self.assertEqual(coach.Region.parse("/x/pjm_at_0x2048_size_4096x2048.tobj"), coach.TILES[1])
        body = coach.body_template(self.cab, region)
        self.assertEqual(body.size, (4096, 2048))
        self.assertEqual(body.getpixel((2048, 500))[3], 255)  # inside the painted quad
        self.assertEqual(body.getpixel((2048, 1900))[3], 0)  # outside it
        film = coach.film_template(self.cab)
        self.assertEqual(film.size, coach.FILM_SIZE)
        self.assertGreater(film.getbbox()[2], 0)
        with self.assertRaises(ValueError):
            coach.load_cabin(self.files.get, "/missing/cab.pmd")

    def test_films(self):
        d, g = coach.with_films(self.cab, "/f/glass.mat", "/f/trim.mat")
        self.assertEqual(pmd.material_table(d)[0][-2:], ["/f/glass.mat", "/f/trim.mat"])
        m = pmg.read(g)
        self.assertEqual(len(m.pieces), 4 + 3)  # window: outside + inside; trim: outside only
        win_out, win_in, trim = m.pieces[4:]
        self.assertEqual((win_out.material, trim.material), (4, 5))
        # lifted 1.5 mm outwards (window normal is -x), the inside copy the other way, reversed
        self.assertAlmostEqual(float(win_out.positions[0, 0]), -1.25 - coach.FILM_LIFT, places=5)
        self.assertAlmostEqual(float(win_in.positions[0, 0]), -1.25 + coach.FILM_LIFT, places=5)
        np.testing.assert_array_equal(win_in.triangles, win_out.triangles[:, ::-1])
        uv = win_out.uvs[0]
        self.assertTrue(((uv >= 0) & (uv <= 1)).all())
        self.assertIn(b"film.tobj", coach.film_material("/x/film.tobj", glossy=True))


class CoachExport(unittest.TestCase):
    def test_bus_skin_with_films(self):
        d, g, mats = build_coach()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            game = root / "game"
            game.mkdir()
            # the game's HashFS archives are replaced by a dict of files (see game_reader below)
            files = {"vehicle/bus/test/cab.pmd": d, "vehicle/bus/test/cab.pmg": g, **mats}
            veh = vehlib.Vehicle("test.coach", "ets2", "Test", "Coach", vehicle_type="Buses")
            veh.variants.append(
                vehlib.Variant(
                    "c1.test.coach.cabin",
                    "Body",
                    mask="pjm_at_0x0_size_4096x2048",
                    model="/vehicle/bus/test/cab.pmd",
                )
            )
            Image.new("RGBA", (256, 128), (0, 128, 0, 255)).save(root / "skin.png")
            Image.new("RGBA", (256, 128), (255, 0, 0, 255)).save(root / "win.png")
            p = Ms2Project.new("ets2")
            p.manifest.vars.set_value("display_name", "Coach test")
            idx = p.add_like("AdvancedTruckSkin", None)
            it = p.items[idx]
            it.set_vehicle(veh)
            it.set("InternalName", "coach1")
            it.set("SkinName", "Coach one")
            e = it.add_entry("CabVariantSkin")
            e.image = str(root / "skin.png")
            e.suitable = ["c1.test.coach.cabin"]
            p.save(root / "p.pb")
            p = Ms2Project.load(root / "p.pb")  # the bus data survives saving
            self.assertTrue(p.items[idx].vehicle().is_bus)
            self.assertEqual(p.items[idx].vehicle().variants[0].mask, "pjm_at_0x0_size_4096x2048")

            it = p.items[idx]
            it.set_extra("PaintWindows", "true")
            tplbuild.configure({})
            probs = {x.message for x in validate(p) if x.level == "error"}
            self.assertTrue(any("windows and trim image" in m for m in probs))
            self.assertTrue(any("game folder" in m for m in probs))
            it.set_extra("WindowsImage", str(root / "win.png"))
            tplbuild.configure({"game_dirs": {"ets2": str(game)}})
            self.addCleanup(tplbuild.configure, None)
            from unittest import mock

            from paintbooth import scanner

            with mock.patch.object(scanner, "game_reader", return_value=files.get):
                out_files, probs = plan_ms2(p)
            self.assertFalse([x for x in probs if x.level == "error"], probs)
            paths = {f.path for f in out_files}
            pj = "vehicle/bus/upgrade/paintjob/test.coach/coach1"
            self.assertIn(f"{pj}/coach1_0/pjm_at_0x0_size_4096x2048.dds", paths)
            self.assertIn("vehicle/bus/test/cab.pmg", paths)
            self.assertIn(f"{pj}/film_glass.mat", paths)
            sii = next(f for f in out_files if f.path.endswith("coach1_0.sii")).data.decode()
            self.assertIn("pjm_at_0x0_size_4096x2048.tobj", sii)
            self.assertIn("def/vehicle/bus/test.coach/paint_job/coach1_0.sii", paths)
            b = Builder(None)
            try:
                res = b.build_files(out_files, root / "out.scs", "fast", root, log=lambda *a: None)
            finally:
                b.close()
            self.assertTrue(res.ok, res.issues)
            with zipfile.ZipFile(root / "out.scs") as z:
                glass = z.read(f"{pj}/film_glass.dds")
            self.assertEqual(glass[:4], b"DDS ")

    def test_mixed_layouts_and_aspect_are_reported(self):
        veh = vehlib.Vehicle("test.coach", "ets2", "Test", "Coach", vehicle_type="Buses")
        veh.variants += [
            vehlib.Variant("a.test.coach.cabin", "A", mask="pjm_at_0x0_size_4096x2048"),
            vehlib.Variant("b.test.coach.cabin", "B", mask="pjm_at_0x2048_size_4096x2048"),
        ]
        with tempfile.TemporaryDirectory() as t:
            Image.new("RGBA", (256, 256)).save(Path(t) / "sq.png")
            p = Ms2Project.new("ets2")
            idx = p.add_like("AdvancedTruckSkin", None)
            it = p.items[idx]
            it.set_vehicle(veh)
            e = it.add_entry("CabVariantSkin")
            e.image = str(Path(t) / "sq.png")
            e.suitable = ["a.test.coach.cabin", "b.test.coach.cabin"]
            msgs = [x.message for x in validate(p)]
        self.assertTrue(any("different template layouts" in m for m in msgs), msgs)
        self.assertTrue(any("coach templates are 2:1" in m for m in msgs), msgs)


if __name__ == "__main__":
    unittest.main()
