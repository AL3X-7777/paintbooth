"""The vehicle library: name resolution, building from game-like archives, packs and installing."""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from paintbooth import library, vehlib

TRUCK = """SiiNunit {
accessory_truck_data : vehicle.acme.road
{
\tinfo[]: "@@brand_acme@@"
\tinfo[]: "@@series_road@@"
}
}"""
CAB = 'SiiNunit { accessory_cabin_data : big.acme.road.cabin { name: "@@cab_big@@" } }'
CAB2 = 'SiiNunit { accessory_cabin_data : small.acme.road.cabin { name: "@@cab_unknown@@" } }'
BUMPER = 'SiiNunit { accessory_addon_data : pnt.acme.road.f_bumper { name: "@@bumper_paint@@" } }'
PAINT = (
    'SiiNunit { accessory_paint_job_data : x.acme.road.paint_job { paint_job_mask: "/x.tobj" }\n'
    'simple_paint_job_data : .ovr0 { acc_list[]: "f_bumper.pnt" } }'
)
BODY = 'SiiNunit { accessory_trailer_body_data : curt_13.acme.box.body { name: "@@body_curtain@@" } }'
BODY2 = 'SiiNunit { accessory_trailer_body_data : curt_15.acme.box.body { name: "@@body_curtain@@" } }'
TRAILER_ACC = 'SiiNunit { accessory_addon_data : al.acme.box.sideskirt { name: "@@skirt_alu@@" } }'
LOCALE = """SiiNunit { localization_db : .localization {
\tkey[]: "brand_acme"
\tval[]: "ACME"
\tkey[]: "series_road"
\tval[]: "Roadster"
\tkey[]: "cab_big"
\tval[]: "Big Cab"
\tkey[]: "bumper_paint"
\tval[]: "Painted"
\tkey[]: "body_curtain"
\tval[]: "Curtainsider"
\tkey[]: "skirt_alu"
\tval[]: "Aluminium"
} }"""


def make_mod(path: Path) -> Path:
    """A ZIP mod with one truck (two cabins, one paintable bumper) and one owned trailer."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("def/vehicle/truck/acme.road/data.sii", TRUCK)
        z.writestr("def/vehicle/truck/acme.road/cabin/big.sii", CAB)
        z.writestr("def/vehicle/truck/acme.road/cabin/small.sii", CAB2)
        z.writestr("def/vehicle/truck/acme.road/accessory/f_bumper/pnt.sii", BUMPER)
        z.writestr("def/vehicle/truck/acme.road/paint_job/x.sii", PAINT)
        z.writestr("def/vehicle/trailer_owned/acme.box/body/c13.sii", BODY)
        z.writestr("def/vehicle/trailer_owned/acme.box/body/c15.sii", BODY2)
        z.writestr("def/vehicle/trailer_owned/acme.box/accessory/sideskirt/al.sii", TRAILER_ACC)
        z.writestr("def/vehicle/truck/scania.r/paint_job/skin.sii", PAINT)  # a skin mod: no cabins
        z.writestr("locale/en_us/local.acme.sii", LOCALE)
    return path


class LocalizerTest(unittest.TestCase):
    def test_tokens(self):
        loc = library.Localizer({"a": "Alpha", "b": "Beta"})
        self.assertEqual(loc("@@a@@"), "Alpha")
        self.assertEqual(loc("@@a@@ @@b@@"), "Alpha Beta")
        self.assertEqual(loc("plain"), "plain")
        self.assertEqual(loc("@@missing@@", "fallback"), "fallback")
        self.assertEqual(loc("", "fallback"), "fallback")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mod = make_mod(self.tmp / "acme.scs")

    def test_build_resolves_names(self):
        lines = []
        vs = {v.id: v for v in library.build(self.mod, "ets2", lines.append)}
        self.assertEqual(set(vs), {"acme.road", "acme.box"})  # the skin mod's scania.r has no cabins
        truck = vs["acme.road"]
        self.assertEqual((truck.brand, truck.name, truck.kind), ("ACME", "Roadster", "truck"))
        self.assertEqual([x.name for x in truck.variants], ["Big Cab", "Small"])  # unknown key: id
        self.assertEqual(
            [(a.value, a.name, a.group) for a in truck.accessories], [("f_bumper.pnt", "Painted", "F Bumper")]
        )
        box = vs["acme.box"]
        self.assertTrue(box.is_trailer)
        self.assertEqual(
            [(x.id, x.name) for x in box.variants],
            [("body.curt_13", "Curtainsider (curt_13)"), ("body.curt_15", "Curtainsider (curt_15)")],
        )
        self.assertEqual([a.value for a in box.accessories], ["sideskirt.al"])
        self.assertEqual(len(lines), 2)

    def test_pack_and_install(self):
        pack = library.write_pack(library.build(self.mod, "ets2"), self.tmp / "pack", "2026.01.01", "1.0")
        self.assertEqual(library.pack_version(pack), "2026.01.01")
        self.assertEqual(json.loads((pack / "index.json").read_text())["vehicles"], 2)
        vehicles = self.tmp / "Vehicles"
        (vehicles / "mine.xml").parent.mkdir(parents=True)
        (vehicles / "mine.xml").write_text("<vehicle id='x.y'><variant id='a'/></vehicle>")
        mods = self.tmp / "mods"
        mods.mkdir()
        make_mod(mods / "acme_trucks.scs")
        (mods / "broken.scs").write_bytes(b"not an archive")
        with mock.patch.object(library, "BUNDLED", pack):
            res = library.install(vehicles, mods, online=False)
        self.assertEqual((res.version, res.vehicles, res.mods, res.downloaded), ("2026.01.01", 2, 2, False))
        self.assertEqual(len(res.mod_errors), 1)
        self.assertTrue((vehicles / "mine.xml").is_file())  # the user's own files stay
        lib, _errors = vehlib.load_library(vehicles)
        self.assertIn("acme.road", {v.id for v in lib.values()})
        # a newer pack from GitHub wins over the bundled one
        newer = library.write_pack(library.build(self.mod, "ets2"), self.tmp / "newer", "2027.01.01")
        with (
            mock.patch.object(library, "BUNDLED", pack),
            mock.patch.object(library, "_download", return_value=newer),
        ):
            res = library.install(vehicles, None)
        self.assertEqual((res.version, res.downloaded, res.mods), ("2027.01.01", True, 0))

    def test_download_reads_a_flat_pack(self):
        pack = library.write_pack(library.build(self.mod, "ets2"), self.tmp / "pack", "2026.02.02")
        buf = self.tmp / "dl.zip"
        with zipfile.ZipFile(buf, "w") as z:
            for f in pack.iterdir():
                z.write(f, f.name)
            z.writestr("../evil.xml", "x")
        with mock.patch.object(library.urllib.request, "urlopen", return_value=open(buf, "rb")):
            got = library._download()
        self.assertEqual(library.pack_version(got), "2026.02.02")
        self.assertFalse((got.parent.parent / "evil.xml").exists())
        with mock.patch.object(library.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(library._download())

    def test_bundled_pack_is_valid(self):
        self.assertTrue(library.pack_version(library.BUNDLED))
        lib, errors = vehlib.load_library(library.BUNDLED)
        self.assertEqual(errors, [])
        self.assertGreater(len(lib), 50)
        self.assertTrue(any(v.is_bus for v in lib.values()))
        self.assertTrue(any(v.is_trailer for v in lib.values()))


if __name__ == "__main__":
    unittest.main()
