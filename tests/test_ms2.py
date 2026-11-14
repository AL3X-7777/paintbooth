"""Tests for .ms2 project support, the Advanced Truck Skin exporter, vehicle library and HashFS reader.

Run with:  python -m unittest discover tests
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from paintbooth import vehlib
from paintbooth.builder import Builder
from paintbooth.formats import dds_bytes, read_tobj_path, tobj_bytes
from paintbooth.hashfs import cityhash64
from paintbooth.ms2 import AdvancedTruckSkin, Ms2Project
from paintbooth.ms2build import plan_ms2, validate
from paintbooth.xmldom import parse, serialize


def demo_vehicle():
    v = vehlib.Vehicle("acme.hauler", "ats", "Acme", "Hauler", "SCS Software", "tests")
    v.variants += [
        vehlib.Variant("day.acme.hauler.cabin", "Day Cab"),
        vehlib.Variant("slp.acme.hauler.cabin", "Sleeper"),
    ]
    v.accessories += [
        vehlib.Accessory("sideskirt.paint", "Paint", "Sideskirt"),
        vehlib.Accessory("bumper.std_pt", "Standard", "Bumper", "", ""),
    ]
    return v


class Ms2Format(unittest.TestCase):
    def test_serializer_matches_dotnet_style(self):
        text = (
            '<?xml version="1.0" encoding="utf-16"?>\r\n<Project xmlns:xsi="x" xsi:type="ScsProject">\r\n'
            '  <A>line1\r\nline2 &amp; &lt;tag&gt;</A>\r\n  <B />\r\n  <C xsi:nil="true" />\r\n'
            "  <D>\r\n    <string>x</string>\r\n  </D>\r\n</Project>"
        )
        raw = text.encode("utf-8")
        self.assertEqual(serialize(parse(raw)), raw)

    def test_new_project_roundtrip_and_edit_revert(self):
        p = Ms2Project.new("ats")
        i = p.add_like("AdvancedTruckSkin", None)
        it = p.items[i]
        it.set_vehicle(demo_vehicle())
        data = p.to_bytes()
        q = Ms2Project(parse(data))
        self.assertEqual(q.to_bytes(), data)
        it2 = q.items[0]
        self.assertIsInstance(it2, AdvancedTruckSkin)
        self.assertEqual(it2.truck_id, "acme.hauler")
        name = it2.get("SkinName")
        it2.set("SkinName", "changed")
        self.assertNotEqual(q.to_bytes(), data)
        it2.set("SkinName", name)
        self.assertEqual(q.to_bytes(), data)
        self.assertEqual(it2.vehicle().variants, demo_vehicle().variants)

    def test_internal_names_and_duplicate(self):
        p = Ms2Project.new("ets2")
        p.add_like("AdvancedTruckSkin", None)
        p.add_like("AdvancedTruckSkin", None)
        # new items start with empty text fields; price and unlock level keep their defaults
        self.assertEqual(p.internal_names(), ["", ""])
        self.assertEqual(p.items[1].get("SkinName"), "")
        self.assertEqual(p.items[1].get("SkinPrice"), "1")
        p.items[0].set("InternalName", "abc0001")
        p.items[1].set("InternalName", "abc0002")
        names = p.internal_names()
        j = p.duplicate(0)
        self.assertNotIn(p.items[j].get("InternalName"), names)
        self.assertEqual(p.game, "ets2")


class Ms2Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Image.new("RGBA", (256, 256), (200, 30, 30, 255)).save(self.root / "cab.png")
        (self.root / "acc.dds").write_bytes(dds_bytes(Image.new("RGBA", (64, 64), (1, 2, 3, 255)), "DXT1"))
        p = Ms2Project.new("ats")
        p.path = self.root / "test.ms2"
        idx = p.add_like("AdvancedTruckSkin", None)
        it = p.items[idx]
        it.set_vehicle(demo_vehicle())
        it.set("InternalName", "abc0000001")
        it.set("SkinName", "Test")
        e = it.add_entry("CabVariantSkin")
        e.image = str(self.root / "cab.png")
        e.suitable = ["day.acme.hauler.cabin", "slp.acme.hauler.cabin"]
        a = it.add_entry("AccessorySkins")
        a.image = "C:\\Users\\someone\\Desktop\\acc.dds"  # moved project: found by file name
        a.suitable = ["sideskirt.paint"]
        it.vars.set_bool("EnableMetallic", True)
        p.manifest.vars.set_value("display_name", "Test mod")
        p.save()
        self.project = Ms2Project.load(p.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_layout(self):
        files, probs = plan_ms2(self.project)
        self.assertFalse([x for x in probs if x.level == "error"], probs)
        out = self.root / "out.scs"
        r = Builder(None, workers=1).build_files(files, out, "fast", self.root, log=lambda *a: None)
        self.assertTrue(r.ok)
        z = zipfile.ZipFile(out)
        names = set(z.namelist())
        base = "vehicle/truck/upgrade/paintjob/acme.hauler/abc0000001"
        for n in (
            "manifest.sii",
            "mod_icon.jpg",
            "mod_description.txt",
            "def/vehicle/truck/acme.hauler/paint_job/abc0000001_0.sii",
            "def/vehicle/truck/acme.hauler/paint_job/accessory/abc0000001_0.sii",
            f"{base}/abc0000001_0.dds",
            f"{base}/abc0000001_0.tobj",
            f"{base}/accessories/acc_0.dds",
            f"{base}/accessories/acc_0.tobj",
            "material/ui/accessory/abc0000001.dds",
            "material/ui/accessory/abc0000001.mat",
        ):
            self.assertIn(n, names)
        pj = z.read("def/vehicle/truck/acme.hauler/paint_job/abc0000001_0.sii").decode()
        self.assertIn("accessory_paint_job_data : abc0000001_0.acme.hauler.paint_job", pj)
        self.assertIn('suitable_for[]: "slp.acme.hauler.cabin"', pj)
        self.assertIn("flipflake: true", pj)
        acc = z.read("def/vehicle/truck/acme.hauler/paint_job/accessory/abc0000001_0.sii").decode()
        self.assertIn("simple_paint_job_data : .ovr0", acc)
        self.assertIn('acc_list[]: "sideskirt.paint"', acc)
        # DDS input is passed through untouched
        self.assertEqual(z.read(f"{base}/accessories/acc_0.dds"), (self.root / "acc.dds").read_bytes())
        # tobj bytes use the verified profiles
        self.assertEqual(
            z.read(f"{base}/abc0000001_0.tobj"), tobj_bytes(f"/{base}/abc0000001_0.dds", profile="skin")
        )
        icon = z.read("material/ui/accessory/abc0000001.dds")
        self.assertEqual(icon[84:88], b"DX10")
        self.assertEqual(struct.unpack_from("<I", icon, 128)[0], 91)
        for n in names:
            if n.endswith(".tobj"):
                self.assertIn(read_tobj_path(z.read(n)).lstrip("/"), names)

    def test_trailer_export_layout(self):
        lib, _ = vehlib.load_library(Path(__file__).parent / "data")
        it = self.project.items[0]
        it.set_vehicle(lib["koegel.cargo"])
        for e in it.entries("CabVariantSkin"):
            e.suitable = ["body.curtain_137", "body.curtain_137w"]
        for e in it.entries("AccessorySkins"):
            e.suitable = ["axles.2_paint"]
        files, probs = plan_ms2(self.project)
        self.assertFalse([x for x in probs if x.level == "error"], probs)
        by = {f.path: f for f in files}
        d = "def/vehicle/trailer_owned/koegel.cargo/paint_job"
        self.assertIn(f"{d}/abc0000001.sii", by)
        pj = by[f"{d}/abc0000001.sii"].data.decode()
        self.assertIn("accessory_paint_job_data : abc0000001.koegel.cargo.paint_job", pj)
        self.assertNotIn("suitable_for", pj)
        acc = by[f"{d}/accessory/abc0000001.sii"].data.decode()
        self.assertIn('acc_list[]: "body.curtain_137w"', acc)
        self.assertIn('acc_list[]: "axles.2_paint"', acc)
        self.assertIn("vehicle/trailer_owned/upgrade/paintjob/koegel.cargo/abc0000001/abc0000001_0.tobj", acc)
        self.assertFalse([p for p in by if p.startswith("def/vehicle/truck/")])

    def test_default_icons_unless_chosen(self):
        skin_icon = self.root / "def_skin.png"
        mod_icon = self.root / "def_mod.jpg"
        Image.new("RGB", (256, 64), (0, 0, 0)).save(skin_icon)
        Image.new("RGB", (276, 162), (0, 0, 0)).save(mod_icon)
        it = self.project.items[0]
        it.set("SkinIcon", "")
        self.project.manifest.vars.set_value("icon", "")
        files, _ = plan_ms2(self.project, None, (skin_icon, mod_icon))
        by = {f.path: f for f in files}
        self.assertEqual(by["material/ui/accessory/abc0000001.dds"].job.src, skin_icon)
        self.assertEqual(by["mod_icon.jpg"].job.src, mod_icon)
        # a chosen icon wins
        it.set("SkinIcon", str(self.root / "cab.png"))
        files, _ = plan_ms2(self.project, None, (skin_icon, mod_icon))
        by = {f.path: f for f in files}
        self.assertEqual(by["material/ui/accessory/abc0000001.dds"].job.src, self.root / "cab.png")

    def test_validation(self):
        it = self.project.items[0]
        it.set("InternalName", "Way Too Long Name")
        Image.new("RGBA", (300, 200)).save(self.root / "npot.png")
        it.entries("CabVariantSkin")[0].image = str(self.root / "npot.png")
        it.entries("AccessorySkins")[0].suitable = []
        msgs = " | ".join(p.message for p in validate(self.project) if p.level == "error")
        self.assertIn("a-z, 0-9", msgs)
        self.assertIn("not a power of two", msgs)
        self.assertIn("tick at least one accessory", msgs)


class Library(unittest.TestCase):
    def test_xml_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            v = demo_vehicle()
            vehlib.save_to_library(v, Path(d))
            lib, errors = vehlib.load_library(Path(d))
            self.assertEqual(errors, [])
            self.assertTrue(lib["acme.hauler"].same_content(v))

    def test_bad_xml_reports_line(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.xml").write_text("<vehicle id='a.b'>\n<variant id='x'\n</vehicle>")
            _, errors = vehlib.load_library(Path(d))
            self.assertTrue(errors and errors[0].startswith("x.xml:"))


class HashFs(unittest.TestCase):
    def test_cityhash64_v103(self):
        # empty string is the constant K2; longer inputs exercise every code path
        self.assertEqual(cityhash64(b""), 0x9AE16A3B2F90404F)
        known = {b"def": None, b"def/vehicle/truck": None, b"a" * 40: None, b"b" * 100: None}
        for s in known:
            self.assertEqual(cityhash64(s), cityhash64(bytes(s)))  # deterministic
        self.assertNotEqual(cityhash64(b"def"), cityhash64(b"deg"))


if __name__ == "__main__":
    unittest.main()
