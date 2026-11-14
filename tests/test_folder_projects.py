"""Run with:  python -m unittest discover tests"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from paintbooth.builder import Builder
from paintbooth.formats import dds_bytes, read_tobj_path, tobj_bytes
from paintbooth.model import load_project, parse_color

# Bytes of a game-compiled .tobj (DXT5 with mips) pointing at /dxt5_mips.dds,
# taken from ConverterPIX's HashFS v2 test data.
GAME_TOBJ_DXT5 = bytes.fromhex(
    "010ab17000000000000000000000000000000000010000000200030303000202"
    "00000000000100000e000000000000002f647874355f6d6970732e646473"
)


def make_project(root: Path, skin_xml: str, vehicle_xml: str | None = None):
    (root / "vehicles").mkdir(parents=True)
    (root / "art").mkdir()
    Image.new("RGBA", (256, 256), (10, 20, 200, 255)).save(root / "art/a.png")
    Image.new("RGBA", (256, 256), (200, 20, 10, 255)).save(root / "art/b.png")
    (root / "vehicles/v.xml").write_text(
        vehicle_xml
        or """
<vehicle id="scania.s_2016" kind="truck" game="ets2">
  <slot id="default" main="true"/>
  <slot id="cab_b"><acc>cabin.b</acc></slot>
</vehicle>"""
    )
    (root / "project.xml").write_text(f"""<project>
  <mod package="testpack" title="T" author="me" game="ets2"/>
  {skin_xml}
</project>""")


class Formats(unittest.TestCase):
    def test_tobj_matches_game_bytes(self):
        self.assertEqual(tobj_bytes("/dxt5_mips.dds", profile="game"), GAME_TOBJ_DXT5)
        self.assertEqual(read_tobj_path(GAME_TOBJ_DXT5), "/dxt5_mips.dds")

    def test_dds_mips_and_roundtrip(self):
        img = Image.new("RGBA", (64, 32), (255, 0, 0, 255))
        data = dds_bytes(img, "DXT5")
        self.assertEqual(data[:4], b"DDS ")
        self.assertEqual(data[84:88], b"DXT5")
        mips = struct.unpack_from("<I", data, 28)[0]
        self.assertEqual(mips, 7)  # 64x32 -> 1x1
        expected = sum(max(1, (64 >> i) // 4) * max(1, (32 >> i) // 4) * 16 for i in range(7))
        self.assertEqual(len(data) - 128, expected)
        back = Image.open(io.BytesIO(data)).convert("RGBA")
        self.assertEqual(back.size, (64, 32))
        r, g, _b, _ = back.getpixel((10, 10))
        self.assertGreater(r, 240)
        self.assertLess(g, 10)

    def test_colors(self):
        self.assertEqual(parse_color("#ffffff"), (1.0, 1.0, 1.0))
        self.assertAlmostEqual(parse_color("#808080")[0], 0.2159, places=3)
        self.assertEqual(parse_color("0.5, 0.25, 0"), (0.5, 0.25, 0.0))
        with self.assertRaises(ValueError):
            parse_color("#zzz")


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_build(self):
        make_project(
            self.root,
            """
  <skin id="blue" name="Blue" price="10" base_color="#ffffff">
    <target vehicle="scania.s_2016" image="art/a.png"/>
    <target vehicle="scania.s_2016" slot="cab_b" image="art/b.png"/>
    <attr name="base_color_locked" value="false"/>
  </skin>""",
        )
        p = load_project(self.root)
        self.assertEqual(p.errors, [])
        b = Builder(self.root / ".cache", workers=1)
        r = b.build(p, log=lambda *_: None)
        self.assertTrue(r.ok)
        z = zipfile.ZipFile(self.root / "build/testpack.scs")
        names = set(z.namelist())
        for n in [
            "manifest.sii",
            "mod_icon.jpg",
            "mod_description.txt",
            "def/vehicle/truck/scania.s_2016/paint_job/blue.sii",
            "def/vehicle/truck/scania.s_2016/paint_job/accessory/blue.sii",
            "material/ui/accessory/testpack_blue.mat",
            "material/ui/accessory/testpack_blue.tobj",
            "material/ui/accessory/testpack_blue.dds",
            "vehicle/truck/upgrade/paintjob/testpack/scania_s_2016/blue/default.dds",
            "vehicle/truck/upgrade/paintjob/testpack/scania_s_2016/blue/cab_b.tobj",
        ]:
            self.assertIn(n, names)
        pj = z.read("def/vehicle/truck/scania.s_2016/paint_job/blue.sii").decode()
        self.assertIn("accessory_paint_job_data : blue.scania.s_2016.paint_job", pj)
        self.assertIn(
            'paint_job_mask: "/vehicle/truck/upgrade/paintjob/testpack/scania_s_2016/blue/default.tobj"', pj
        )
        self.assertIn("airbrush: true", pj)
        self.assertIn("base_color_locked: false", pj)
        acc = z.read("def/vehicle/truck/scania.s_2016/paint_job/accessory/blue.sii").decode()
        self.assertIn("simple_paint_job_data : .ovr0", acc)
        self.assertIn('acc_list[]: "cabin.b"', acc)
        # every tobj points at a DDS that exists in the mod
        for n in names:
            if n.endswith(".tobj"):
                self.assertIn(read_tobj_path(z.read(n)).lstrip("/"), names)
        # rebuild without changes uses the cache
        r2 = b.build(load_project(self.root), log=lambda *_: None)
        self.assertEqual((r2.textures_encoded, r2.textures_cached), (0, r.textures_encoded))

    def test_errors_reported_with_lines(self):
        make_project(
            self.root,
            """
  <skin id="This Is Too Long!" name="x">
    <target vehicle="scania.s_2016" image="art/a.png"/>
  </skin>
  <skin id="ok" price="abc">
    <target vehicle="nope.truck" image="art/missing.png"/>
    <target vehicle="scania.s_2016" slot="cab_z" image="art/a.png"/>
  </skin>""",
        )
        p = load_project(self.root)
        msgs = "\n".join(str(i) for i in p.errors)
        self.assertIn("project.xml:4: skin id", msgs)
        self.assertIn("not a whole number", msgs)
        self.assertIn("image not found: art/missing.png", msgs)
        self.assertIn("has no slot 'cab_z'", msgs)
        self.assertFalse(Builder(None).build(p, log=lambda *_: None).ok)

    def test_xml_syntax_error_does_not_crash(self):
        make_project(self.root, "<skin id='a'>", vehicle_xml="<vehicle id='a.b'")
        p = load_project(self.root)
        self.assertTrue(any("XML syntax error" in i.message for i in p.errors))

    def test_needs_main_slot(self):
        make_project(
            self.root,
            """
  <skin id="x"><target vehicle="scania.s_2016" slot="cab_b" image="art/a.png"/></skin>""",
        )
        p = load_project(self.root)
        self.assertTrue(any("main slot" in i.message for i in p.errors))


if __name__ == "__main__":
    unittest.main()


class DxtQuality(unittest.TestCase):
    """Anti-aliased, partly transparent line art must survive DXT5 compression (alpha edges)."""

    def test_alpha_edges(self):
        import io

        from PIL import Image, ImageChops, ImageDraw, ImageStat

        from paintbooth.formats import dds_bytes

        big = Image.new("RGBA", (2048, 512), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        for k in range(5):
            d.line((0, 40 + k * 80, 2047, 160 + k * 80), fill=(30, 30, 30, 255), width=12)
        im = big.resize((512, 128), Image.LANCZOS)
        back = Image.open(io.BytesIO(dds_bytes(im, "DXT5", True)))
        back.load()
        diff = ImageChops.difference(im.getchannel("A"), back.getchannel("A"))
        self.assertLess(ImageStat.Stat(diff).mean[0], 1.0)
        self.assertLessEqual(diff.getextrema()[1], 30)
        # fully transparent pixels stay fully transparent
        zero = im.getchannel("A").point(lambda v: 255 if v == 0 else 0)
        self.assertEqual(ImageStat.Stat(back.getchannel("A"), mask=zero).extrema[0][1], 0)
