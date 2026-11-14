"""Project model and IO: xmldom, ms2, ms2build, ms2skeleton, newproject, appdirs, names, scanner, ....

Also covers vehlib and updates.

Regression tests for review findings F7, F12, F13, F15, F24, F25, F26, F32, F33 and F34.
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

from paintbooth import appdirs, ms2build, names, scanner, updates, vehlib
from paintbooth.ms2 import AdvancedTruckSkin, Ms2Project, TemplateItem, _num, item_class
from paintbooth.ms2skeleton import new_manifest, new_project
from paintbooth.newproject import create_project, safe_package
from paintbooth.xmldom import E, Elem, parse, serialize


def demo_vehicle(trailer=False):
    v = vehlib.Vehicle("acme.hauler", "ats", "Acme", "Hauler", "SCS Software", "tests")
    if trailer:
        v.vehicle_type = "Trailers"
    v.variants += [vehlib.Variant("day.acme.hauler.cabin", "Day Cab")]
    v.accessories += [vehlib.Accessory("sideskirt.paint", "Paint", "Sideskirt")]
    return v


def skin_project(root: Path, name="abc", trailer=False) -> Ms2Project:
    """A valid one-skin project saved in ``root``."""
    Image.new("RGBA", (64, 64), (200, 30, 30, 255)).save(root / "cab.png")
    p = Ms2Project.new("ats")
    p.path = root / "test.pb"
    idx = p.add_like("AdvancedTruckSkin", None)
    it = p.items[idx]
    it.set_vehicle(demo_vehicle(trailer))
    it.set("InternalName", name)
    it.set("SkinName", "Test")
    e = it.add_entry("CabVariantSkin")
    e.image = str(root / "cab.png")
    e.suitable = ["day.acme.hauler.cabin"]
    p.manifest.vars.set_value("display_name", "Test mod")
    p.save()
    return p


# ------------------------------------------------------------------ xmldom (F7)
class XmlDom(unittest.TestCase):
    def test_invalid_control_characters_are_dropped(self):
        root = E("Project", E("A", text="pasted from Word\x0bline\x01\x00end"))
        root.attrs["note"] = "x\x1fy"
        back = parse(serialize(root))
        self.assertEqual(back.get_text("A"), "pasted from Wordlineend")
        self.assertEqual(back.attrs["note"], "xy")

    def test_crlf_is_not_doubled(self):
        for text in ("a\r\nb", "a\nb", "a\rb"):
            out = serialize(E("R", E("A", text=text)))
            self.assertIn(b"a\r\nb", out)
            self.assertNotIn(b"\r\r", out)
            self.assertEqual(parse(out).get_text("A"), "a\nb")

    def test_roundtrip_bytes_unchanged(self):
        data = (
            '<?xml version="1.0" encoding="utf-16"?>\r\n<Project a="1 &amp; &quot;2&quot;">\r\n'
            "  <A>x &lt; y\r\nz</A>\r\n  <B />\r\n  <C>\r\n    <D>ü</D>\r\n  </C>\r\n</Project>"
        ).encode()
        self.assertEqual(serialize(parse(data)), data)
        self.assertEqual(serialize(parse(b"\xef\xbb\xbf" + data)), data)

    def test_attribute_breaks_and_tabs_survive(self):
        root = Elem("R", attrs={"a": "x\ny\tz\r"})
        self.assertEqual(parse(serialize(root)).attrs["a"], "x\ny\tz\r")

    def test_utf16_input_and_navigation(self):
        root = parse("<R><A><B>t</B></A><A/></R>".encode("utf-16"))
        self.assertEqual(root.path("A", "B").text, "t")
        self.assertIsNone(root.path("A", "X", "Y"))
        self.assertEqual(len(root.findall("A")), 2)
        self.assertEqual(root.get_text("Missing", "d"), "d")
        root.set_text("Z", "1", before="A")
        self.assertEqual(root.children[0].tag, "Z")
        root.set_text("Y", "2", before="Nope")
        self.assertEqual(root.children[-1].tag, "Y")
        self.assertIn("Elem R", repr(root))
        self.assertEqual(E("V", type="T").type(), "T")


# ------------------------------------------------------------------ ms2
class Ms2Model(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_with_control_characters_reopens(self):  # F7
        p = Ms2Project.new("ets2")
        p.manifest.vars.set_value("description", "pasted\x0bline\r\nnext")
        f = self.root / "x.pb"
        p.save(f)
        back = Ms2Project.load(f)
        self.assertEqual(back.manifest.vars.value("description"), "pastedline\nnext")
        self.assertFalse(back.dirty)

    def test_save_without_path_raises(self):
        with self.assertRaises(ValueError):
            Ms2Project.new().save()

    def test_load_rejects_other_xml(self):
        f = self.root / "x.ms2"
        f.write_text("<NotAProject/>")
        with self.assertRaises(ValueError):
            Ms2Project.load(f)

    def test_next_internal_name_respects_limit(self):  # F25
        p = Ms2Project.new()
        self.assertEqual(p.next_internal_name("abcdefghij99", limit=10), "abcdefg100")
        self.assertEqual(p.next_internal_name("abcdefghij99"), "abcdefghi100")
        self.assertEqual(p.next_internal_name("skin07"), "skin08")
        self.assertEqual(p.next_internal_name("myskin"), "myskin0001")

    def test_duplicate_long_skin_name_stays_valid(self):  # F25
        p = skin_project(self.root, name="abcdefgh99")
        new = p.duplicate(0)
        name = p.items[new].get("InternalName")
        self.assertLessEqual(len(name), 10)
        self.assertNotEqual(name, "abcdefgh99")
        self.assertFalse([x for x in ms2build.validate(p) if x.field == "InternalName"])

    def test_ets2_manifest_points_at_ets2_folder(self):  # F32
        root = new_project("ets2")
        self.assertIn("\\ETS2\\", root.path("ScsModManifest", "TemplateBaseFolder").text)
        self.assertIn("\\ATS\\", new_manifest().get_text("TemplateBaseFolder"))
        self.assertEqual(Ms2Project.new("ets2").game, "ets2")

    def test_items_edit_and_display(self):
        p = skin_project(self.root)
        it = p.items[0]
        self.assertIsInstance(it, AdvancedTruckSkin)
        self.assertIn("acme.hauler", it.display())
        it.vars.set_color("BaseColor", (0.5, 1, 0.25))
        self.assertEqual(it.vars.color("BaseColor"), (0.5, 1.0, 0.25))
        self.assertEqual([c.tag for c in it.vars.get("BaseColor").children][-3:], ["B", "G", "R"])
        self.assertEqual(it.vars.color("Nope"), (0.0, 0.0, 0.0))
        it.vars.set_child_value("SkinIcon", "OverlaySource", "o.png")
        self.assertEqual(it.vars.child_value("SkinIcon", "OverlaySource"), "o.png")
        it.vars.set_child_value("SkinIcon", "OverlaySource", "p.png")
        self.assertEqual(it.vars.child_value("SkinIcon", "OverlaySource"), "p.png")
        self.assertEqual(it.vars.child_value("Nope", "x"), "")
        self.assertEqual(p.manifest.vars.predefined("category")[0][0], "truck")
        p.manifest.vars.set_strings("compatible_versions", ["1.50.*"])
        self.assertEqual(p.manifest.vars.strings("compatible_versions"), ["1.50.*"])
        a = it.add_entry("AccessorySkins")
        a.suitable = ["sideskirt.paint"]
        it.remove_entry("AccessorySkins", 0)
        self.assertEqual(it.entries("AccessorySkins"), [])
        self.assertEqual(set(p.embedded_vehicles()), {"acme.hauler"})
        self.assertEqual(p.move(0, 5), 0)
        idx = p.add_like("AdvancedTruckSkin", None)  # copies the existing skin, cleared
        self.assertEqual(p.items[idx].get("InternalName"), "")
        self.assertIsNone(p.add_like("SomethingElse", None))
        p.delete(idx)
        self.assertEqual(p.manifest.display(), "Mod manifest")
        self.assertEqual(_num("0.50"), "0.5")
        self.assertEqual(_num(3.0), "3")

    def test_template_items(self):
        tdef = parse(
            '<Template xsi:type="LightBox"><TemplateName>Box</TemplateName><Variables>'
            '<Variable xsi:type="IntegerVariable"><Name>Price</Name><Validators>'
            '<Validator xsi:type="NumberRangeValidator"><MinValue>1</MinValue></Validator>'
            "</Validators><Value>0</Value></Variable>"
            '<Variable xsi:type="ListItemVariable"><Name>Pick</Name><Validators /><Value>a</Value>'
            "<PredefinedValues><ListItem><ChildVariables><Variable><Name>K</Name></Variable></ChildVariables>"
            "<Text>Option B</Text><Value>b</Value></ListItem></PredefinedValues></Variable>"
            '<Variable xsi:type="StringArrayVariable"><Name>Arr</Name><Validators /></Variable>'
            "</Variables></Template>"
        )
        p = Ms2Project.new()
        with mock.patch("paintbooth.ms2.template_supported", return_value=True):
            idx = p.add_template(tdef, Path("C:/MS2"))
            it = p.items[idx]
            self.assertIsInstance(it, TemplateItem)
            self.assertIs(item_class("LightBox"), TemplateItem)
        self.assertEqual(it.type, "LightBox")
        self.assertEqual(it.get("Price"), "1")  # raised to the minimum
        self.assertTrue(it.get("Env.ApplicationDirectory").endswith("\\"))
        it.set_choice("Pick", "b")
        self.assertEqual(it.var("Pick").path("ChildVariables", "Variable").get_text("Name"), "K")
        it.set("Price", 5)
        self.assertEqual(it.get("Price"), "5")
        it.set_array("Arr", ["x", "y"])
        self.assertEqual([c.text for c in it.var("Arr").find("Value").children], ["x", "y"])
        it.set_array("Arr", [])
        self.assertEqual(it.var("Arr").find("Value").text, "")
        self.assertEqual(it.internal_name(), "")
        self.assertEqual(len(it.variables()), 4)


# ------------------------------------------------------------------ ms2build
class Ms2Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ms2build.clear_resolve_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nan_and_malformed_numbers_are_problems(self):  # F26
        p = skin_project(self.root)
        it = p.items[0]
        it.vars.get("BaseColor").set_text("R", "nan")
        it.vars.set_bool("EnableMetallic", True)
        it.set("FlakeDensity", "abc")
        it.vars.get("FlipColor").set_text("G", "1,5")
        files, probs = ms2build.plan_ms2(p)  # must not raise
        self.assertEqual(files, [])
        fields = {x.field for x in probs if x.level == "error"}
        self.assertTrue({"BaseColor", "FlakeDensity", "FlipColor"} <= fields, fields)

    def test_no_compatible_versions_unless_typed(self):  # F33
        p = skin_project(self.root)
        files, probs = ms2build.plan_ms2(p)
        self.assertFalse([x for x in probs if x.level == "error"], probs)
        man = next(f for f in files if f.path == "manifest.sii").data.decode()
        self.assertNotIn("compatible_versions", man)
        p.manifest.vars.set_strings("compatible_versions", ["1.50.*"])
        files, _ = ms2build.plan_ms2(p)
        man = next(f for f in files if f.path == "manifest.sii").data.decode()
        self.assertIn('compatible_versions[]: "1.50.*"', man)
        p.manifest.vars.set_strings("compatible_versions", ["1.5*"])
        self.assertIn("compatible_versions", {x.field for x in ms2build.validate(p)})

    def test_trailer_and_overrides(self):
        p = skin_project(self.root, trailer=True)
        it = p.items[0]
        a = it.add_entry("AccessorySkins")
        a.image = str(self.root / "cab.png")
        a.suitable = ["sideskirt.paint"]
        it.vars.set_bool("EnableMask", True)
        files, probs = ms2build.plan_ms2(p, {"display_name": "Override"}, default_icons=None)
        paths = {f.path for f in files}
        self.assertIn("def/vehicle/trailer_owned/acme.hauler/paint_job/abc.sii", paths)
        man = next(f for f in files if f.path == "manifest.sii").data.decode()
        self.assertIn('"Override"', man)
        unit = next(f for f in files if f.path.endswith("paint_job/abc.sii")).data.decode()
        self.assertIn("mask_r_color", unit)
        self.assertTrue(all(x.level == "warning" for x in probs))

    def test_validation_messages(self):
        p = skin_project(self.root, name="BAD")
        p.manifest.vars.set_value("display_name", "")
        p.manifest.vars.set_value("GlobalInternalName", "Bad-Name")
        p.manifest.vars.set_value("icon", "missing.jpg")
        it = p.items[0]
        it.set("SkinPrice", "x")
        it.set("SkinIcon", "nope.png")
        e = it.add_entry("CabVariantSkin")
        e.image = str(self.root / "npot.png")
        Image.new("RGB", (60, 64)).save(self.root / "npot.png")
        e2 = it.add_entry("CabVariantSkin")
        e2.image = "gone.png"
        it.add_entry("CabVariantSkin")
        (self.root / "bad.png").write_bytes(b"not an image")
        it.add_entry("CabVariantSkin").image = str(self.root / "bad.png")
        msgs = " | ".join(x.message for x in ms2build.validate(p))
        for want in (
            "Mod name is empty",
            "a-z, 0-9",
            "Mod icon not found",
            "Must be a whole number",
            "Icon not found",
            "not a power of two",
            "file not found",
            "no image selected",
            "cannot read bad.png",
            "tick at least one cabin",
        ):
            self.assertIn(want, msgs)
        p.items[0].set("InternalName", "abcdefghijk")
        self.assertIn("longer than 10", " ".join(x.message for x in ms2build.validate(p)))
        p.items[0].set("InternalName", "")
        self.assertIn("internal name is empty", " ".join(x.message for x in ms2build.validate(p)))

    def test_duplicate_names_and_unsupported(self):
        p = skin_project(self.root)
        p.duplicate(0)
        p.items[1].set("InternalName", "abc")
        p.templates_el.children.append(Elem("Template", None, {"xsi:type": "Weird"}))
        msgs = " ".join(x.message for x in ms2build.validate(p))
        self.assertIn("also used by item 1", msgs)
        self.assertIn("not supported yet", msgs)

    def test_resolve_finds_moved_files_and_caches_the_index(self):  # F12
        sub = self.root / "deep" / "er"
        sub.mkdir(parents=True)
        (sub / "skin [v2].png").write_bytes(b"x")
        self.assertEqual(ms2build.resolve("C:\\other\\skin [v2].png", self.root), sub / "skin [v2].png")
        self.assertIsNone(ms2build.resolve("", self.root))
        self.assertIsNone(ms2build.resolve("C:\\x\\nothing.png", None))
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("walked again")):
            for _ in range(50):  # typing a path: no new walk while the index is fresh
                self.assertIsNone(ms2build.resolve("C:\\x\\missing.png", self.root))
            self.assertEqual(ms2build.resolve("C:\\other\\skin [v2].png", self.root), sub / "skin [v2].png")
        # a new file is found once the index expires
        (sub / "late.png").write_bytes(b"x")
        with mock.patch.object(ms2build.time, "monotonic", return_value=time.monotonic() + 60):
            self.assertEqual(ms2build.resolve("late.png", self.root / "nowhere"), None)
            self.assertEqual(ms2build.resolve("C:\\a\\late.png", self.root), sub / "late.png")
        # a cached hit that was deleted rebuilds at once
        (sub / "late.png").unlink()
        self.assertIsNone(ms2build.resolve("C:\\a\\late.png", self.root))
        # relative to the project and as the same tree under the project folder
        (self.root / "Users").mkdir()
        (self.root / "Users" / "u.png").write_bytes(b"x")
        self.assertEqual(ms2build.resolve("C:\\Users\\u.png", self.root), self.root / "Users" / "u.png")

    def test_index_is_bounded(self):
        for i in range(ms2build._INDEX_MAX_FOLDERS + 3):
            d = self.root / str(i)
            d.mkdir()
            ms2build.resolve("C:\\x.png", d)
        self.assertLessEqual(len(ms2build._indexes), ms2build._INDEX_MAX_FOLDERS)
        with mock.patch.object(ms2build, "_INDEX_MAX_ENTRIES", 1):
            (self.root / "0" / "a").write_bytes(b"")
            (self.root / "0" / "b").write_bytes(b"")
            self.assertEqual(len(ms2build._build_index(self.root / "0").names), 1)

    def test_image_size_cache(self):
        f = self.root / "a.png"
        Image.new("RGB", (32, 16)).save(f)
        self.assertEqual(ms2build.image_size(f), (32, 16))
        with mock.patch.object(ms2build, "_read_image_size", side_effect=AssertionError):
            self.assertEqual(ms2build.image_size(f), (32, 16))
        self.assertIsNone(ms2build.image_size(self.root / "missing.png"))

    def test_default_icons_cached_and_rerendered(self):
        a = ms2build._defaults(None)
        self.assertTrue(all(p.is_file() for p in a))
        self.assertTrue(str(a[0]).startswith(os.environ["PAINTBOOTH_HOME"]))
        a[0].unlink()
        self.assertTrue(ms2build._defaults(None)[0].is_file())
        given = (Path("s.png"), Path("m.jpg"))
        self.assertIs(ms2build._defaults(given), given)

    def test_names_and_versions(self):
        self.assertEqual(ms2build.next_version("1.09", "1.09"), "1.10")
        self.assertEqual(ms2build.next_version("x", None), "1.00")
        self.assertEqual(ms2build.next_version("v2.05", None), "2.05")
        self.assertEqual(ms2build.creator_suffix("Mod", " "), "Mod")
        self.assertEqual(ms2build.creator_suffix("Mod by A", "A"), "Mod by A")
        self.assertEqual(ms2build.version_suffix("", "1"), "V1")
        self.assertEqual(ms2build.version_suffix("M V1", "1"), "M V1")
        self.assertEqual(ms2build.version_suffix("M", ""), "M")


class Names(unittest.TestCase):  # F15
    def test_token_rules(self):
        self.assertIsNone(names.validate_token("ok_1"))
        self.assertEqual(names.validate_token("", "Package"), "Package is empty")
        self.assertIn("a-z", names.validate_token("Ab"))
        self.assertIn("longer than 12", names.validate_token("a" * 13))
        self.assertTrue(names.is_token("a" * 13, max_len=None))
        self.assertFalse(names.is_token("a" * 11, max_len=10))
        self.assertTrue(names.TOKEN_RE.match("a" * 12))


# ------------------------------------------------------------------ newproject (F24, F33)
class NewProject(unittest.TestCase):
    def test_escaping_ascii_package_and_no_compatible(self):
        from paintbooth.model import TOKEN_RE, load_project

        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "Skins für LKW"
            f = create_project(d, title='Tom & Jerry "<1>"', author="A&B")
            text = f.read_text(encoding="utf-8")
            self.assertNotIn('compatible="1.5*"', text)
            self.assertNotIn("compatible=", text.split("<mod ", 1)[1].split("/>", 1)[0])
            p = load_project(d)
            self.assertEqual(p.errors, [])
            self.assertEqual(p.mod.title, 'Tom & Jerry "<1>"')
            self.assertEqual(p.mod.author, "A&B")
            self.assertEqual(p.mod.package, "skins_fur_lk")
            self.assertTrue(TOKEN_RE.match(p.mod.package))
            self.assertEqual(p.mod.compatible, [])
            with self.assertRaises(FileExistsError):
                create_project(d)
        self.assertEqual(safe_package("Ærø-Straße"), "aero_strasse")
        self.assertEqual(safe_package("日本"), "my_skins")


# ------------------------------------------------------------------ appdirs (F13)
class AppDirs(unittest.TestCase):
    def test_everything_inside_custom_home(self):
        home = Path(os.environ["PAINTBOOTH_HOME"])
        for p in (
            appdirs.settings_file(),
            appdirs.cache_dir(),
            appdirs.projects_dir(),
            appdirs.vehicles_dir(),
            appdirs.exports_dir("ets2"),
            appdirs.exports_dir("ats"),
            appdirs.generated_dir(),
            appdirs.templates_dir("ets2"),
        ):
            self.assertTrue(p.is_relative_to(home), p)
        self.assertEqual(appdirs.old_homes(), [])
        self.assertFalse(appdirs.migrate())
        self.assertEqual(appdirs.moved_path("C:/x"), "C:/x")
        self.assertEqual(appdirs.ensure(), home)
        self.assertTrue((home / "Templates" / "ATS").is_dir())
        self.assertIn(home / "Fonts", appdirs.font_dirs())

    def test_migrate_and_settings_copy_without_custom_home(self):
        with tempfile.TemporaryDirectory() as t:
            docs = Path(t) / "Documents"
            (docs / "Skinyard" / "Projects").mkdir(parents=True)
            (docs / "Skinyard" / "Projects" / "a.pb").write_text("a")
            env = {k: v for k, v in os.environ.items() if k not in ("PAINTBOOTH_HOME", "SKINYARD_HOME")}
            env["HOME"] = t
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(Path, "home", return_value=Path(t)),
                # on Windows documents() asks the shell for the real folder; the logic under test
                # is the migration, so point it at the temporary one there
                mock.patch.object(appdirs, "documents", return_value=docs)
                if sys.platform == "win32"
                else contextlib.nullcontext(),
            ):
                self.assertEqual(appdirs.documents(), docs)
                self.assertTrue(appdirs.migrate())
                self.assertTrue((docs / "Paintbooth" / "Projects" / "a.pb").is_file())
                # both exist: missing files are moved, existing ones kept
                (docs / "Skinyard" / "Projects").mkdir(parents=True)
                (docs / "Skinyard" / "Projects" / "a.pb").write_text("old")
                (docs / "Skinyard" / "Projects" / "b.pb").write_text("b")
                self.assertTrue(appdirs.migrate())
                self.assertEqual((docs / "Paintbooth" / "Projects" / "a.pb").read_text(), "a")
                self.assertTrue((docs / "Paintbooth" / "Projects" / "b.pb").is_file())
                # the later name "Paint booth" is moved too, and wins over Skinyard files
                (docs / "Paint booth" / "Projects").mkdir(parents=True)
                (docs / "Paint booth" / "Projects" / "c.pb").write_text("c")
                self.assertTrue(appdirs.migrate())
                self.assertTrue((docs / "Paintbooth" / "Projects" / "c.pb").is_file())
                self.assertFalse((docs / "Paint booth").exists())
                spaced = str(docs / "Paint booth" / "Exports")
                self.assertEqual(appdirs.moved_path(spaced), str(docs / "Paintbooth" / "Exports"))
                other = str(docs / "Paint booth") + "2/x"  # a different folder that only starts the same
                self.assertEqual(appdirs.moved_path(other), other)
                old = str(docs / "Skinyard" / "x.png")
                self.assertEqual(appdirs.moved_path(old), str(docs / "Paintbooth" / "x.png"))
                (Path(t) / ".skinyard.json").write_text(json.dumps({"recent": [old]}))
                s = appdirs.settings_file()
                self.assertEqual(s, Path(t) / ".paintbooth.json")
                self.assertEqual(json.loads(s.read_text())["recent"], [str(docs / "Paintbooth" / "x.png")])

    def test_icons(self):
        img = appdirs.render_icon((64, 16), "AB12")
        self.assertEqual(img.size, (64, 16))
        self.assertIsNone(appdirs.find_font(None, name="no_such_font_xyz"))
        with tempfile.NamedTemporaryFile(suffix=".ttf") as f:
            self.assertEqual(appdirs.find_font(f.name), Path(f.name))
        skin, mod = appdirs.default_icons(None)
        with Image.open(skin) as a, Image.open(mod) as b:
            self.assertEqual((a.size, b.size), (appdirs.SKIN_ICON_SIZE, appdirs.MOD_ICON_SIZE))


# ------------------------------------------------------------------ scanner (F34)
SII_TRUCK = """SiiNunit
{
accessory_truck_data : acme.hauler.data
{
    name: "Hauler"   # comment
}
}"""
SII_CABIN = 'SiiNunit {\naccessory_cabin_data : day.acme.hauler.cabin {\n name: "Day Cab"\n}\n}'
SII_PJ = """SiiNunit {
/* block comment */
simple_paint_job_data : .ovr0 {
    acc_list[]: "cabin"
    acc_list[]: "sideskirt.paint"
}
simple_paint_job_data : .ovr1 {
    acc_list[]: "bumper.std"
}
}"""
SII_ACC = 'SiiNunit {\naccessory_addon_data : paint.acme.hauler.sideskirt {\n name: "Paint skirt"\n}\n}'


def fake_def(root: Path) -> Path:
    base = root / "def" / "vehicle" / "truck" / "acme.hauler"
    (base / "cabin").mkdir(parents=True)
    (base / "paint_job").mkdir()
    (base / "accessory").mkdir()
    (base / "data.sii").write_text(SII_TRUCK)
    (base / "cabin" / "day.sii").write_text(SII_CABIN)
    (base / "paint_job" / "pj.sii").write_text(SII_PJ.replace("\n", "\n"))
    (base / "accessory" / "a.sii").write_text(SII_ACC)
    (base / "paint_job" / "enc.sii").write_bytes(b"ScsC....")
    bad = root / "def" / "vehicle" / "truck" / "Bad-Id"
    bad.mkdir()
    (bad / "x.sii").write_text("")
    return root


class Scanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_comments_and_units(self):
        self.assertEqual(scanner.strip_comments('a: "x#y" # c\nb // d'), 'a: "x#y" \nb ')
        units = list(scanner.units(SII_PJ))
        self.assertEqual(units[0][2]["acc_list[]"], ["cabin", "sideskirt.paint"])

    def test_scan_folder_and_zip(self):
        src = fake_def(self.root / "src")
        out = self.root / "out"
        lines = []
        written = scanner.scan(src, out, game="ats", log=lines.append)
        self.assertEqual([p.name for p in written], ["acme_hauler.xml"])
        text = written[0].read_text()
        self.assertIn('name="Hauler"', text)
        self.assertIn("<acc>sideskirt.paint</acc>", text)
        self.assertIn("bumper.std", text)
        self.assertIn("1 encrypted/binary", text)
        self.assertTrue(any("skip Bad-Id" in x for x in lines))
        self.assertEqual(scanner.scan(src, out, log=lines.append), [])  # exists: skipped
        self.assertEqual(len(scanner.scan(src, out, overwrite=True, log=lines.append)), 1)

        z = self.root / "mod.scs"
        with zipfile.ZipFile(z, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src).as_posix())
        written = scanner.scan(z, self.root / "out2", log=lines.append)
        self.assertEqual(len(written), 1)
        self.assertEqual(scanner.scan(self.root / "out2", self.root / "o3", log=lines.append), [])

    def test_hashfs_input_is_a_value_error(self):  # F34
        f = self.root / "def.scs"
        f.write_bytes(b"SCS#" + b"\0" * 60)
        with self.assertRaises(scanner.HashFsArchiveError) as cm:
            scanner.scan(f, self.root / "out")
        self.assertIsInstance(cm.exception, ValueError)

    def test_scan_vehicles(self):
        src = fake_def(self.root / "src")
        vs = scanner.scan_vehicles(src, "ats", log=lambda *_: None)
        self.assertEqual(len(vs), 1)
        v = vs[0]
        self.assertEqual((v.id, v.name, v.brand), ("acme.hauler", "Hauler", "Acme"))
        self.assertEqual([x.id for x in v.variants], ["day.acme.hauler.cabin"])
        acc = {a.value: a.name for a in v.accessories}
        self.assertEqual(acc["sideskirt.paint"], "Paint skirt")
        self.assertEqual(acc["bumper.std"], "Std")
        z = self.root / "mod.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("def/vehicle/truck/acme.hauler/cabin/day.sii", SII_CABIN)
        self.assertEqual(len(scanner.scan_vehicles(z, log=lambda *_: None)), 1)
        game = self.root / "game"
        game.mkdir()
        (game / "def.scs").write_bytes(b"junk")  # unreadable archive is skipped
        self.assertEqual(scanner.scan_vehicles(game, log=lambda *_: None), [])


# ------------------------------------------------------------------ vehlib
class VehLib(unittest.TestCase):
    def test_library_formats_and_duplicates(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            v = demo_vehicle()
            p = vehlib.save_to_library(v, d, comment="a & b")
            (d / "copy.xml").write_text(p.read_text())
            (d / "multi.xml").write_text(
                '<vehicles><vehicle id="x.y"><variant id="c"/><sound value="s"/></vehicle>'
                "<vehicle/></vehicles>"
            )
            (d / "other.xml").write_text("<foo/>")
            (d / "broken.xml").write_text("<vehicle")
            (d / "ms2.xml").write_text(
                '<?xml version="1.0" encoding="utf-16"?><Variable><InternalName></InternalName></Variable>'
            )
            lib, errors = vehlib.load_library(d)
            self.assertIn("acme.hauler  [acme.hauler.xml]", lib)
            self.assertIn("acme.hauler  [copy.xml]", lib)
            self.assertEqual(lib["x.y"].sounds, [("", "", "s")])
            text = " ".join(errors)
            for want in ("needs an id", "not a vehicle file", "XML syntax error", "<InternalName>"):
                self.assertIn(want, text)
            self.assertTrue(lib.with_id("acme.hauler", "ats"))
            self.assertIsNotNone(lib.match(v))
            self.assertIsNone(lib.match(vehlib.Vehicle("none")))
        self.assertEqual(vehlib.load_library(Path("/nonexistent")), (vehlib.Library(), []))
        self.assertEqual(vehlib.pretty("@@brand_kogel@@ @@series_kogel_cool@@"), "Kogel Cool")
        self.assertEqual(v.display, "SCS Software - Hauler")
        self.assertEqual(v.kind, "truck")

    def test_text_encodings(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.xml"
            for enc in ("utf-16", "utf-8-sig", "utf-8", "cp1252"):
                vehlib.write_text(f, "<a>é</a>\n", enc)
                text, _enc = vehlib.read_text(f)
                self.assertEqual(text.replace("\r\n", "\n"), "<a>é</a>\n")
            f.write_bytes(b"<a>\xe9</a>")
            self.assertEqual(vehlib.read_text(f)[1], "cp1252")


# ------------------------------------------------------------------ updates
class Updates(unittest.TestCase):
    def _reply(self, data):
        body = io.BytesIO(json.dumps(data).encode())
        cm = mock.MagicMock()
        cm.__enter__.return_value = body
        return cm

    def test_latest_release_and_check(self):
        rel = {"tag_name": "v9.0.0", "html_url": "https://example.invalid/r"}
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: self._reply(rel)):
            self.assertEqual(updates.latest_release(), ("9.0.0", updates.RELEASES_URL))
            self.assertEqual(updates.check("0.10.0"), ("9.0.0", updates.RELEASES_URL))
            self.assertIsNone(updates.check("10.0"))
        with mock.patch("urllib.request.urlopen", return_value=self._reply({**rel, "prerelease": True})):
            self.assertIsNone(updates.latest_release())
        with mock.patch("urllib.request.urlopen", return_value=self._reply([1, 2])):
            self.assertIsNone(updates.latest_release())
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(updates.check("0.1"))
        self.assertFalse(updates.is_newer("x", "1.0"))
        self.assertTrue(updates.is_newer("1.0.1", "1.0"))


if __name__ == "__main__":
    unittest.main()
