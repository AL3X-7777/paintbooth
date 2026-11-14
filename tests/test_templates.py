"""Template matching, template-pack import, library duplicates."""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from paintbooth import vehlib


class _NoFlagInfo(zipfile.ZipInfo):
    """Like many zip tools: UTF-8 bytes in the name but no UTF-8 flag."""

    __slots__ = ()

    def _encodeFilenameFlags(self):
        return self.filename.encode("utf-8"), self.flag_bits & ~0x800


class Templates(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._old = os.environ["PAINTBOOTH_HOME"]
        os.environ["PAINTBOOTH_HOME"] = str(self.home)
        from paintbooth import templates

        templates.clear_cache()
        self.t = templates

    def tearDown(self):
        os.environ["PAINTBOOTH_HOME"] = self._old
        shutil.rmtree(self.home)

    def _zip(self, names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(_NoFlagInfo(n), b"x")
        p = self.home / "ETS2_templates.zip"
        p.write_bytes(buf.getvalue())
        return p

    def test_import_and_match(self):
        z = self._zip(
            [
                "ETS2 templates/Trucks/Scania R/PSD/Scania R.psd",
                "ETS2 templates/Trucks/Scania R 2009/PSD/Scania R 2009.psd",
                "ETS2 templates/Trailers/Kögel Cargo/PSD/Kögel Cargo.psd",
                "ETS2 templates/Trailers/Kögel Cargo/PNG/Kögel Cargo.png",
            ]
        )
        self.assertEqual(self.t.guess_game(z.name), "ets2")
        self.t.import_pack(z, "ets2")
        rels = sorted(td.rel for td in self.t.index("ets2"))
        self.assertIn("Trailers/Kögel Cargo", rels)  # name decoded, PSD/PNG folded
        veh = vehlib.Vehicle
        new_r = veh("scania.r_2016", "ets2", "Scania", "R")
        old_r = veh("scania.r", "ets2", "Scania", "R 2009")
        kogel = veh("koegel.cargo", "ets2", "Kögel", "Kögel Cargo", vehicle_type="Trailers")
        self.assertEqual(self.t.find(new_r, "ets2").rel, "Trucks/Scania R")
        self.assertEqual(self.t.find(old_r, "ets2").rel, "Trucks/Scania R 2009")
        td = self.t.find(kogel, "ets2")
        self.assertEqual(td.rel, "Trailers/Kögel Cargo")
        self.assertEqual([p.name for p in td.psds], ["Kögel Cargo.psd"])
        self.assertIsNone(self.t.find(veh("volvo.fh", "ets2", "Volvo", "FH"), "ets2"))
        # a remembered choice wins
        over = {"ets2:volvo.fh": str(self.home / "Templates" / "ETS2" / "Trucks" / "Scania R")}
        self.assertEqual(self.t.find(veh("volvo.fh", "ets2"), "ets2", over).rel, "Trucks/Scania R")


class LibraryDuplicates(unittest.TestCase):
    def test_same_id_in_two_files_are_both_kept(self):
        d = Path(tempfile.mkdtemp())
        try:
            for fname, game, n in (("ATS.scs.box.xml", "ats", 1), ("ETS2.scs.box.xml", "ets2", 2)):
                v = vehlib.Vehicle(
                    "scs.box",
                    game,
                    "SCS",
                    "Box",
                    variants=[vehlib.Variant("body.x", "X")],
                    accessories=[vehlib.Accessory(f"a.{i}", "A") for i in range(n)],
                )
                (d / fname).write_text(vehlib.to_xml(v), encoding="utf-8")
            lib, _errors = vehlib.load_library(d)
            self.assertEqual(len(lib), 2)
            self.assertEqual(len(lib.with_id("scs.box", "ets2")), 1)
            self.assertEqual(len(lib.with_id("scs.box", "ets2")[0].accessories), 2)
            self.assertTrue(vehlib.Vehicle("x", "all").fits_game("ats"))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
