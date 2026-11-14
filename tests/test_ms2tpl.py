"""Tests for Mods Studio 2 template items (lightbox, billboard, ... style items).

A tiny, made-up template is written to a temporary "Mods Studio 2" folder, so no MS2 files are
needed. Run with:  python -m unittest discover tests
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from paintbooth import ms2data, pmd, tplbuild
from paintbooth.formats import read_tobj_path, tobj_bytes
from paintbooth.hashfs import cityhash64
from paintbooth.ms2 import Ms2Project, TemplateItem
from paintbooth.ms2build import export_name, plan_ms2, validate, version_suffix
from paintbooth.xmldom import parse


def make_pmd(looks: list[list[str]]) -> bytes:
    """A minimal version-4 .pmd: N looks x M material slots, one variant, nothing else."""
    n_looks, n_mats = len(looks), len(looks[0])
    look_off = 64
    var_off = look_off + 8 * n_looks
    mat_off = var_off + 8
    data_off = mat_off + 4 * n_looks * n_mats
    blob, offs = bytearray(), []
    for row in looks:
        for m in row:
            offs.append(data_off + len(blob))
            blob += m.encode() + b"\0"
    head = struct.pack(
        "<16I",
        4,
        n_mats,
        n_looks,
        0,
        1,
        0,
        0,
        0,
        len(blob),
        look_off,
        var_off,
        mat_off,
        mat_off,
        mat_off,
        mat_off,
        data_off,
    )
    names = b"".join(struct.pack("<Q", 1 + i + 38 * 1) for i in range(n_looks))
    return head + names + struct.pack("<Q", 0) + struct.pack(f"<{len(offs)}I", *offs) + bytes(blob)


TEMPLATE = r"""<?xml version="1.0" encoding="utf-16"?>
<Template xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xsi:type="TruckLightbox">
  <BuildActions>
    <Action xsi:type="CreateDirectory"><Paths><string>vehicle\x\{{InternalName}}\</string></Paths></Action>
    <Action xsi:type="CopyFile">
      <Paths>
        <CopyPair><From>{{Truck}}\model\</From><To>vehicle\x\{{InternalName}}\</To></CopyPair>
        <CopyPair><From>{{Truck}}\unit.sii</From><To>def\x\{{InternalName}}.sii</To></CopyPair>
      </Paths>
    </Action>
    <Action xsi:type="UpdateTextVariables"><Paths><string>def\x\{{InternalName}}.sii</string></Paths></Action>
    <Action xsi:type="ModifyPmdMaterials">
      <FilePath>vehicle\x\{{InternalName}}\box.pmd</FilePath>
      <MaterialsPath>{{Truck}}\</MaterialsPath>
      <ReplaceData><ReplaceWith>/x/{{InternalName}}/</ReplaceWith><SearchFor>/x/</SearchFor></ReplaceData>
      <DeleteProcessedMatFiles>false</DeleteProcessedMatFiles>
    </Action>
    <Action xsi:type="CleanPmdLooks"><PmdPaths><string>vehicle\x\{{InternalName}}\box.pmd</string></PmdPaths></Action>
    <Action xsi:type="ModifyTobjFile">
      <FilePaths><string>vehicle\x\{{InternalName}}\*.tobj</string></FilePaths>
      <ReplaceData><ReplaceWith>/x/{{InternalName}}/</ReplaceWith><SearchFor>/x/</SearchFor></ReplaceData>
    </Action>
    <Action xsi:type="WriteTextFile"><FileName>def\list.sii</FileName><TextLines><string>SiiNunit</string><string>{</string></TextLines></Action>
    <Action xsi:type="ProcessArrayValues">
      <ArrayItemName>IMAGE</ArrayItemName><ArrayIndexName>INDEX</ArrayIndexName>
      <BuildActions>
        <Action xsi:type="NextPmdLook"><PmdPaths><string>vehicle\x\{{InternalName}}\box.pmd</string></PmdPaths><LookIndexName>LOOK</LookIndexName></Action>
        <Action xsi:type="ImportImage">
          <Destination>vehicle\x\{{InternalName}}\skin_{{INDEX:D2}}.dds</Destination>
          <OutputFormat><Format>Dds</Format><Preset>ScsSkinImage</Preset></OutputFormat>
          <Source>{{IMAGE}}</Source>
        </Action>
        <Action xsi:type="GenerateTobjFile">
          <Content>/vehicle/x/{{InternalName}}/skin_{{INDEX:D2}}.dds</Content>
          <FileName>vehicle\x\{{InternalName}}\skin_{{INDEX:D2}}.tobj</FileName>
          <TobjType>TruckSkin</TobjType>
        </Action>
        <Action xsi:type="AppendTextFile"><FileName>def\list.sii</FileName><TextLines><string>  item[]: {{LOOK:D2}} {{LATER}}</string></TextLines></Action>
      </BuildActions>
      <IndexOffset>1</IndexOffset>
      <VariableName>Images</VariableName>
    </Action>
    <Action xsi:type="AppendTextFile"><FileName>def\list.sii</FileName><TextLines><string>}</string></TextLines></Action>
    <Action xsi:type="ConditionalAction">
      <BuildActions>
        <Action xsi:type="WriteTextFile"><FileName>def\big.sii</FileName><TextLines><string>big {{Paint}}</string></TextLines></Action>
      </BuildActions>
      <FalseBuildActions>
        <Action xsi:type="WriteTextFile"><FileName>def\small.sii</FileName><TextLines><string>small</string></TextLines></Action>
      </FalseBuildActions>
      <ComparisonValues><string>big_truck</string></ComparisonValues>
      <Comparison>Equals</Comparison>
      <ExecuteIf>[Truck]</ExecuteIf>
    </Action>
    <Action xsi:type="ImportImage">
      <Destination>material\ui\{{InternalName}}.dds</Destination>
      <OutputFormat><Format>Dds</Format><Preset>ScsIconImage</Preset></OutputFormat>
      <Source>{{Icon}}</Source>
    </Action>
  </BuildActions>
  <TemplateBaseFolder>\Data\ModProjects\ETS2\Templates\TruckLightbox\</TemplateBaseFolder>
  <TemplateDisplayPattern>[[Lightbox |]] {{Truck}} {{Name}}</TemplateDisplayPattern>
  <TemplateName>Truck lightbox</TemplateName>
  <Variables>
    <Variable xsi:type="ListItemVariable">
      <ChildVariables />
      <Group>Truck</Group><IsEnabled>true</IsEnabled><Name>Truck</Name><Text>Select truck</Text>
      <Validators><Validator xsi:type="NotEmptyValidator"><AdviceMessage>Please select a value.</AdviceMessage></Validator></Validators>
      <PredefinedValues>
        <anyType xsi:type="ListItemVariable">
          <ChildVariables><Variable xsi:type="StringVariable"><ChildVariables /><IsEnabled>true</IsEnabled><Name>InternalName</Name><Value>acme.big</Value></Variable></ChildVariables>
          <Group>Acme</Group><IsEnabled>true</IsEnabled><Text>Big</Text><Value>big_truck</Value><PredefinedValuesSelected>false</PredefinedValuesSelected>
        </anyType>
        <anyType xsi:type="ListItemVariable">
          <ChildVariables />
          <Group>Acme</Group><IsEnabled>true</IsEnabled><Text>Small</Text><Value>small_truck</Value><PredefinedValuesSelected>false</PredefinedValuesSelected>
        </anyType>
      </PredefinedValues>
      <PredefinedValuesSelected>false</PredefinedValuesSelected>
    </Variable>
    <Variable xsi:type="StringVariable">
      <ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>InternalName</Name><Text>Internal Name</Text>
      <Validators>
        <Validator xsi:type="NotEmptyValidator"><AdviceMessage>Please enter a value.</AdviceMessage></Validator>
        <Validator xsi:type="ScsInternalNameValidator" />
        <Validator xsi:type="ScsDuplicatedInternalNameValidator"><RegisterScope>InternalName</RegisterScope></Validator>
      </Validators>
    </Variable>
    <Variable xsi:type="StringVariable"><ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>Name</Name><Text>Name</Text></Variable>
    <Variable xsi:type="TwoOptionsVariable">
      <ChildVariables /><EnabledSource>[Truck]={big_truck}</EnabledSource><Group>basic</Group><IsEnabled>true</IsEnabled>
      <Name>Paint</Name><Text>Painted</Text><Value>false</Value><TrueValue>paint</TrueValue><FalseValue>plastic</FalseValue>
    </Variable>
    <Variable xsi:type="IntegerVariable">
      <ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>Price</Name><Text>Price</Text>
      <Validators><Validator xsi:type="NumberRangeValidator"><MaxValue xsi:nil="true" /><MinValue>1</MinValue></Validator></Validators>
      <Value>0</Value>
    </Variable>
    <Variable xsi:type="ScsImageSourceArrayVariable">
      <ChildVariables /><Group>images</Group><IsEnabled>true</IsEnabled><Name>Images</Name><Text>Images</Text>
      <Validators><Validator xsi:type="ItemCountValidator"><MaxCount>3</MaxCount><MinCount>1</MinCount></Validator></Validators>
      <AllowEditing>false</AllowEditing><DisplayMode>Auto</DisplayMode><PredefinedValuesSelected>false</PredefinedValuesSelected>
    </Variable>
    <Variable xsi:type="ScsImageSourceVariable">
      <ChildVariables /><Group>icon</Group><IsEnabled>true</IsEnabled><Name>Icon</Name><Text>Icon</Text>
      <Validators>
        <Validator xsi:type="NotEmptyValidator"><AdviceMessage>Please select an image.</AdviceMessage></Validator>
        <Validator xsi:type="ImageSizeValidator"><Width>256</Width><Height>64</Height></Validator>
      </Validators>
      <AllowedExtensions><string>png</string></AllowedExtensions><PreloadDefault>false</PreloadDefault>
      <UseDefaultOptions><string>default_icon.png</string></UseDefaultOptions>
    </Variable>
  </Variables>
</Template>"""  # noqa: E501 (a real MS2 template, one element per line as MS2 writes it)

MAT = (
    'effect : "eut2.dif.rfx" {\n\ttexture : "texture_base" {\n'
    '\t\tsource : "/vehicle/x/skin_%02d.tobj"\n\t}\n}\n'
)


class TemplateItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ms2 = root / "Mods Studio 2"
        t = self.ms2 / "Data/ModProjects/ETS2/Templates/TruckLightbox"
        (t / "big_truck/model").mkdir(parents=True)
        (t / "template.xml").write_text(TEMPLATE, encoding="utf-8")
        Image.new("RGBA", (256, 64), (9, 9, 9, 255)).save(t / "default_icon.png")
        (t / "big_truck/unit.sii").write_text(
            'unit : {{InternalName}}.{{Truck.InternalName}} { name: "{{Name}}" price: {{Price}} }'
        )
        looks = []
        for k in (1, 2, 3):
            path = f"/automat/aa/{k:016x}.mat"
            (t / "big_truck" / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
            (t / "big_truck" / path.lstrip("/")).write_text(MAT % k)
            looks.append([path])
        (t / "big_truck/model/box.pmd").write_bytes(make_pmd(looks))
        (t / "big_truck/model/box.pmg").write_bytes(b"geometry")
        (t / "big_truck/model/paint.tobj").write_bytes(tobj_bytes("/vehicle/x/paint.dds"))
        self.imgs = []
        for i in range(2):
            p = root / f"img{i}.png"
            Image.new("RGBA", (64, 64), (200, i * 100, 0, 255)).save(p)
            self.imgs.append(str(p))
        tplbuild.configure({"ms2_dir": str(self.ms2)})

    def tearDown(self):
        tplbuild.configure(None)
        self.tmp.cleanup()

    def project(self):
        p = Ms2Project.new("ets2")
        p.manifest.vars.set_value("display_name", "Test")
        tdef = ms2data.template(self.ms2, "ets2", "TruckLightbox")
        i = p.add_template(tdef.root, self.ms2)
        it = p.items[i]
        return p, it

    def test_pmd_round_trip_and_looks(self):
        data = make_pmd([["/automat/aa/1.mat"], ["/automat/aa/2.mat"], ["/automat/aa/3.mat"]])
        self.assertEqual(pmd.rewrite(data), data)
        self.assertEqual(len(pmd.look_names(data)), 3)
        out = pmd.rewrite(data, {"/automat/aa/1.mat": "/automat/bb/longer_name_here.mat"}, keep_looks=2)
        self.assertEqual(
            pmd.material_table(out),
            [
                ["/automat/bb/longer_name_here.mat"],
                ["/automat/aa/2.mat"],
                ["/automat/bb/longer_name_here.mat"],
            ],
        )

    def test_new_item_and_validation(self):
        p, it = self.project()
        self.assertIsInstance(it, TemplateItem)
        self.assertEqual(it.get("Price"), "1")  # raised to the validator's minimum
        self.assertEqual(it.display(), "Truck lightbox")
        msgs = " ".join(x.message for x in validate(p))
        self.assertIn("Please select a value", msgs)
        self.assertIn("Please enter a value", msgs)
        self.assertNotIn("Please select an image", msgs)  # the icon has a template default
        it.set_choice("Truck", "big_truck")
        it.set("InternalName", "Bad Name")
        it.set("Name", "My box")
        self.assertEqual(it.display(), "Truck lightbox | Big My box")
        self.assertIn("a-z, 0-9", " ".join(x.message for x in validate(p)))

    def test_build(self):
        p, it = self.project()
        it.set_choice("Truck", "big_truck")
        it.set("InternalName", "mybox")
        it.set("Name", "My box")
        it.set("Paint", "true")
        it.set_array("Images", self.imgs)
        self.assertEqual(validate(p), [])
        files, _probs = plan_ms2(p)
        f = {x.path: x for x in files}
        self.assertEqual(
            f["def/x/mybox.sii"].data.decode(), 'unit : mybox.acme.big { name: "My box" price: 1 }'
        )
        self.assertEqual(
            f["def/list.sii"].data.decode(),
            "SiiNunit\r\n{\r\n  item[]: 01 {{LATER}}\r\n  item[]: 02 {{LATER}}\r\n}\r\n",
        )
        self.assertEqual(f["def/big.sii"].data.decode(), "big paint\r\n")
        self.assertNotIn("def/small.sii", f)
        self.assertEqual(read_tobj_path(f["vehicle/x/mybox/paint.tobj"].data), "/vehicle/x/mybox/paint.dds")
        self.assertEqual(
            read_tobj_path(f["vehicle/x/mybox/skin_02.tobj"].data), "/vehicle/x/mybox/skin_02.dds"
        )
        self.assertEqual(f["vehicle/x/mybox/skin_01.dds"].job.purpose, "mask")
        self.assertEqual(f["vehicle/x/mybox/box.pmg"].job.purpose, "copy")
        icon = f["material/ui/mybox.dds"].job
        self.assertEqual((icon.purpose, icon.size, icon.src.name), ("icon", (256, 64), "default_icon.png"))
        # materials: rewritten, named by CityHash64 of their text; only the 2 used looks, look 3 -> look 1
        table = pmd.material_table(f["vehicle/x/mybox/box.pmd"].data)
        self.assertEqual(table[2], table[0])
        for k, row in enumerate(table[:2], 1):
            text = f[row[0].lstrip("/")].data
            self.assertIn(f"/vehicle/x/mybox/skin_{k:02d}.tobj".encode(), text)
            h = f"{cityhash64(text):016x}"
            self.assertEqual(row[0], f"/automat/{h[:2]}/{h}.mat")
        self.assertEqual(len([k for k in f if k.startswith("automat/")]), 2)

    def test_build_whole_mod_and_reload(self):
        p, it = self.project()
        it.set_choice("Truck", "big_truck")
        it.set("InternalName", "mybox")
        it.set_array("Images", self.imgs[:1])
        out = Path(self.tmp.name) / "mod.scs"
        from paintbooth.builder import Builder

        files, _ = plan_ms2(p)
        res = Builder(Path(self.tmp.name) / "cache").build_files(
            files, out, "fast", out.parent, log=lambda *a: None
        )
        self.assertTrue(res.ok, res.issues)
        with zipfile.ZipFile(out) as z:
            self.assertIn("vehicle/x/mybox/skin_01.dds", z.namelist())
            self.assertEqual(z.read("vehicle/x/mybox/box.pmg"), b"geometry")
        # saved as .pb and opened again: same item, same values
        pb = Path(self.tmp.name) / "proj.pb"
        p.save(pb)
        q = Ms2Project.load(pb)
        self.assertEqual(q.items[0].get("InternalName"), "mybox")
        self.assertEqual(q.items[0].array("Images"), self.imgs[:1])
        self.assertEqual(parse(pb.read_bytes()).tag, "Project")

    def test_existing_mod(self):
        src = Path(self.tmp.name) / "other.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("def/a.sii", "A")
            z.writestr("manifest.sii", "theirs")
        tdir = self.ms2 / "Data/ModProjects/ETS2/Templates/ExistingMod"
        tdir.mkdir(parents=True)
        (tdir / "template.xml").write_text(
            r"""<Template xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ExistingMod">
  <BuildActions><Action xsi:type="ExtractScsArchive"><Paths><STPair><Source>{{ModSource}}</Source><Target>\</Target></STPair></Paths></Action></BuildActions>
  <TemplateBaseFolder>\Data\ModProjects\ETS2\Templates\ExistingMod\</TemplateBaseFolder>
  <Variables><Variable xsi:type="FileSourceVariable"><ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>ModSource</Name><Text>Mod file</Text></Variable></Variables>
</Template>""",  # noqa: E501 (MS2 template text kept as MS2 writes it)
            encoding="utf-8",
        )
        p = Ms2Project.new("ets2")
        p.manifest.vars.set_value("display_name", "Mine")
        i = p.add_template(ms2data.template(self.ms2, "ets2", "ExistingMod").root, self.ms2)
        it = p.items[i]
        it.set("ModSource", str(src))
        files, _ = plan_ms2(p)
        f = {x.path: x for x in files}
        self.assertEqual(f["def/a.sii"].data, b"A")
        self.assertIn(b"Mine", f["manifest.sii"].data)  # our manifest wins


class NameAndVersion(unittest.TestCase):
    def test_version_suffix(self):
        self.assertEqual(version_suffix("My mod by AL3X", "1.02"), "My mod by AL3X V1.02")
        self.assertEqual(version_suffix("My mod", "v2"), "My mod v2")
        self.assertEqual(version_suffix("My mod V1.00", "1.00"), "My mod V1.00")
        self.assertEqual(version_suffix("My mod", ""), "My mod")

    def test_export_name(self):
        s = {"append_creator": True, "creator_name": "AL3X", "append_version": True}
        self.assertEqual(export_name("Skins", s, "1.01"), "Skins by AL3X V1.01")
        self.assertEqual(export_name("Skins", {"append_version": True}, "V1.00"), "Skins V1.00")
        self.assertEqual(export_name("Skins", {}, "1.00"), "Skins")


class Updates(unittest.TestCase):
    def test_versions(self):
        from paintbooth.updates import is_newer, parse_version

        self.assertEqual(parse_version("v0.10.0"), (0, 10, 0))
        self.assertTrue(is_newer("0.10.0", "0.9.0"))
        self.assertTrue(is_newer("v1.0", "0.10.5"))
        self.assertFalse(is_newer("0.10.0", "0.10.0"))
        self.assertFalse(is_newer("0.10", "0.10.0"))
        self.assertFalse(is_newer("garbage", "0.10.0"))


if __name__ == "__main__":
    unittest.main()
