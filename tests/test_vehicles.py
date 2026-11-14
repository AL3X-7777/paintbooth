"""MS2-format vehicle files, XML editor insertions, trailer export layout."""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import shutil
import tempfile
import unittest
from pathlib import Path

from paintbooth import vehlib

DATA = Path(__file__).parent / "data"


class VehicleFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        shutil.copy(DATA / "ETS2.koegel.cargo.xml", self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_ms2_vehicle_file_loads(self):
        lib, errors = vehlib.load_library(self.dir)
        self.assertEqual(errors, [])
        v = lib["koegel.cargo"]
        self.assertEqual(v.game, "ets2")
        self.assertTrue(v.is_trailer)
        self.assertEqual(len(v.variants), 4)
        self.assertEqual(v.variants[0].description, "13.7m Curtainsider")
        self.assertEqual(len(v.accessories), 49)

    def test_add_accessory_keeps_file_valid_and_grouped(self):
        text = vehlib.read_text(self.dir / "ETS2.koegel.cargo.xml")[0].replace("\r\n", "\n")
        e = vehlib.add_accessory_edit(text, "Test", "chs_f_l.test", "Chassis Front Left")
        new = vehlib.apply_edit(text, e)
        v = vehlib.from_ms2_node(vehlib.parse_text(new))
        self.assertEqual(len(v.accessories), 50)
        groups = [a.group for a in v.accessories]
        i = [a.value for a in v.accessories].index("chs_f_l.test")
        self.assertEqual(groups[i - 1], "Chassis Front Left")
        self.assertEqual(groups[i + 1], "Chassis Front Right")
        # new group goes at the end, special characters escaped
        e = vehlib.add_accessory_edit(new, "A & B", "x.y", "New <group>")
        v = vehlib.from_ms2_node(vehlib.parse_text(vehlib.apply_edit(new, e)))
        self.assertEqual((v.accessories[-1].name, v.accessories[-1].group), ("A & B", "New <group>"))

    def test_add_variant(self):
        text = vehlib.read_text(self.dir / "ETS2.koegel.cargo.xml")[0]
        e = vehlib.add_variant_edit(text.replace("\r\n", "\n"), "Kögel Cargo", "body.new", "New body")
        v = vehlib.from_ms2_node(vehlib.parse_text(vehlib.apply_edit(text.replace("\r\n", "\n"), e)))
        self.assertEqual(v.variants[-1].id, "body.new")

    def test_skinyard_format_edits(self):
        x = vehlib.to_xml(
            vehlib.Vehicle(
                "a.b",
                "ats",
                "A",
                "B",
                variants=[vehlib.Variant("c.a.b.cabin", "C")],
                accessories=[vehlib.Accessory("x.y", "Y", "G")],
            )
        )
        x = vehlib.apply_edit(x, vehlib.add_accessory_edit(x, "Z", "x.z", "G"))
        x = vehlib.apply_edit(x, vehlib.add_variant_edit(x, "D", "d.a.b.cabin"))
        (self.dir / "a.xml").write_text(x, encoding="utf-8")
        lib, _errors = vehlib.load_library(self.dir)
        self.assertEqual([a.value for a in lib["a.b"].accessories], ["x.y", "x.z"])
        self.assertEqual(len(lib["a.b"].variants), 2)


if __name__ == "__main__":
    unittest.main()
