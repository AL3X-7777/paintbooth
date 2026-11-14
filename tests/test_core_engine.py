"""Core build engine: PMD errors, .NET regexes, builder fixes, caches and HashFS reading.

Covers review findings F5, F6, F9, F10, F11, F16, F22, F29 and F35, plus round trips of the
ZIP writer, the DXT encoder and HashFS v1/v2 archives built here from scratch.
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import io
import logging
import os
import struct
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

from PIL import Image

import paintbooth.builder as bmod
from paintbooth import bcn, formats, hashfs, model, ms2data, pmd, templates, tplbuild, util
from paintbooth.builder import Builder, OutFile, TextureJob, plan
from paintbooth.hashfs import HashFs, cityhash64
from paintbooth.model import load_project, parse_color
from paintbooth.ms2tpl import (
    Engine,
    TemplateError,
    compile_dotnet,
    dotnet_regex,
    expand_dotnet,
    read_archive,
)
from paintbooth.packer import pack_member, write_zip
from paintbooth.xmldom import parse

QUIET = lambda *a: None  # noqa: E731
logging.getLogger("paintbooth").setLevel(logging.ERROR)


def make_pmd(looks: list[list[str]]) -> bytes:
    """A minimal version-4 .pmd: N looks x M material slots (same layout as test_ms2tpl)."""
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
    fields = [4, n_mats, n_looks, 0, 1, 0, 0, 0, len(blob), look_off, var_off, mat_off, mat_off, mat_off]
    head = struct.pack("<16I", *fields, mat_off, data_off)
    names = b"".join(struct.pack("<Q", 1 + i + 38) for i in range(n_looks))
    return head + names + struct.pack("<Q", 0) + struct.pack(f"<{len(offs)}I", *offs) + bytes(blob)


def engine(actions: str, tdir: Path, files=None) -> Engine:
    xml = (
        '<Template xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><BuildActions>'
        + actions
        + "</BuildActions></Template>"
    )
    return Engine(parse(xml), tdir, lambda t: None, files)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


# ====================================================================== PMD (F9)


class PmdErrors(unittest.TestCase):
    def test_valid_file(self):
        data = make_pmd([["/a.mat", "/b.mat"], ["/c.mat", "/a.mat"]])
        self.assertEqual(pmd.material_table(data), [["/a.mat", "/b.mat"], ["/c.mat", "/a.mat"]])
        self.assertEqual(pmd.materials(data), ["/a.mat", "/b.mat", "/c.mat"])
        self.assertEqual(len(pmd.look_names(data)), 2)
        self.assertEqual(pmd.token_str(0), "")
        out = pmd.rewrite(data, {"/a.mat": "/z.mat"}, keep_looks=1)
        self.assertEqual(pmd.material_table(out), [["/z.mat", "/b.mat"], ["/z.mat", "/b.mat"]])

    def test_damaged_files_raise_pmd_error(self):
        good = make_pmd([["/a.mat"]])
        cases = {
            "short": b"\4\0\0\0",
            "version": struct.pack("<16I", 5, *([0] * 15)),
            "no NUL": struct.pack("<16I", 4, 1, 1, 0, 0, 0, 0, 0, 4, 64, 0, 0, 0, 0, 72, 76)
            + struct.pack("<Q", 0)
            + struct.pack("<I", 76)
            + b"abcd",
            "table outside": struct.pack("<16I", 4, 5, 5, *([0] * 13)),
            "offset outside": good[:-8] + struct.pack("<I", 10**6) + good[-4:],
            "bad utf8": good[:-7] + b"\xff\xfe\xfd\xfc\xfb\xfa\0",
            "look outside": struct.pack("<16I", 4, 0, 3, 0, 0, 0, 0, 0, 0, 60, 0, 0, 0, 0, 64, 64),
            "header offset": struct.pack("<16I", 4, 0, 0, 0, 0, 0, 0, 0, 0, 999, 0, 0, 0, 0, 64, 64),
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(pmd.PmdError):
                pmd.material_table(data) if name != "look outside" else pmd.look_names(data)

    def test_rewrite_layout_checks(self):
        good = make_pmd([["/a.mat"]])
        with self.assertRaises(pmd.PmdError):
            pmd.rewrite(good + b"extra")  # strings are not the last block

    def test_engine_turns_pmd_errors_into_template_errors(self):
        e = engine("", Path("."), {"m.pmd": b"\4\0\0\0" * 20})
        e.pmds["m.pmd"] = __import__("paintbooth.ms2tpl", fromlist=["_PmdJob"])._PmdJob()
        with self.assertRaises(TemplateError) as cm:
            e.finish_models()
        self.assertIn("m.pmd", str(cm.exception))


# ====================================================================== .NET regex (F10)


class DotnetRegex(unittest.TestCase):
    def test_translation(self):
        self.assertEqual(dotnet_regex(r"(?<n>a)\k<n>"), r"(?P<n>a)(?P=n)")
        self.assertEqual(dotnet_regex(r"(?'x'a)"), r"(?P<x>a)")
        self.assertEqual(dotnet_regex(r"(?<=a)(?<!b)"), r"(?<=a)(?<!b)")
        self.assertTrue(compile_dotnet(r"(?<word>[a-z]+)").fullmatch("abc"))

    def test_invalid_and_long_patterns(self):
        with self.assertRaises(TemplateError):
            compile_dotnet("(unclosed")
        with self.assertRaises(TemplateError):
            compile_dotnet("a" * 5000)

    def test_replacement_syntax(self):
        m = compile_dotnet(r"(?<k>\w+)=(\d+)").search("x key=42 y")
        self.assertEqual(expand_dotnet("$2:${k}:$$:$&:$0", m), "42:key:$:key=42:key=42")
        self.assertEqual(expand_dotnet("${nope}$9", m), "${nope}$9")  # unknown groups stay
        self.assertEqual(expand_dotnet(r"a\1b", m), r"a\1b")  # backslashes are literal
        self.assertEqual(expand_dotnet("plain", m), "plain")

    def test_replace_action_uses_dotnet_replacements(self):
        e = engine(
            '<Action xsi:type="ReplaceFileText"><FilePaths><string>a.sii</string></FilePaths>'
            "<MultiReplaceData><R><SearchForRegex>/cargo/(?&lt;n&gt;\\d+)/</SearchForRegex>"
            "<ReplaceWith>/c/${n}/$1/</ReplaceWith></R>"
            "<R><SearchFor>X</SearchFor><ReplaceWith>$1</ReplaceWith></R></MultiReplaceData></Action>",
            Path("."),
            {"a.sii": b"/cargo/12/ X"},
        )
        self.assertEqual(e.run()["a.sii"], b"/c/12/12/ $1")

    def test_bad_regex_in_template_is_template_error(self):
        e = engine(
            '<Action xsi:type="ReplaceFileText"><FilePaths><string>a</string></FilePaths>'
            "<ReplaceData><SearchForRegex>([</SearchForRegex><ReplaceWith>x</ReplaceWith></ReplaceData>"
            "</Action>",
            Path("."),
        )
        with self.assertRaises(TemplateError):
            e.run()

    def test_regex_validator_problems(self):
        probs = []
        ck = tplbuild._Check(0, lambda *a: probs.append(a) or a, lambda n: None, {}, [])
        el = parse("<V/>")

        def var(text):
            return tplbuild._Var(el, "N", "Name", "StringVariable", text, None, False)

        for pat, text, expect in (
            (r"(?<v>\d+)\.\d+", "1.5", None),
            (r"(?<v>\d+)\.\d+", "x", "not in the expected format"),
            ("([", "x", "can't be used"),
        ):
            probs.clear()
            tplbuild._check_regex(ck, var(text), parse(f"<V><RegexPattern>{escape(pat)}</RegexPattern></V>"))
            if expect is None:
                self.assertEqual(probs, [])
            else:
                self.assertIn(expect, probs[0][3])


# ====================================================================== engine actions


class EngineActions(TempDir):
    def test_text_actions_and_loops(self):
        (self.root / "model").mkdir()
        (self.root / "model" / "a.tobj").write_bytes(formats.tobj_bytes("/x/a.dds"))
        (self.root / "model" / "b.txt").write_text("{{Name}}")
        item = parse(
            '<Template xmlns:xsi="x"><Variables>'
            '<Variable xsi:type="StringVariable"><Name>Name</Name><Value>box</Value></Variable>'
            '<Variable xsi:type="StringArrayVariable"><Name>L</Name><Value><string>p</string>'
            "<string>q</string></Value></Variable>"
            '<Variable xsi:type="TwoOptionsVariable"><Name>Yes</Name><Value>True</Value>'
            "<TrueValue>Y</TrueValue><FalseValue>N</FalseValue></Variable>"
            '<Variable xsi:type="FloatVariable"><Name>F</Name><Value>1.5</Value>'
            "<FormatString>F2</FormatString></Variable>"
            "</Variables><BuildActions>"
            '<Action xsi:type="CreateDirectory"><Paths><string>x</string></Paths></Action>'
            '<Action xsi:type="CopyFile"><Paths><P><From>model\\</From><To>m\\</To></P>'
            "<P><From>model\\*.txt</From><To>t\\</To></P><P><From>\\m\\</From><To>m2\\</To></P></Paths>"
            "</Action>"
            '<Action xsi:type="UpdateTextVariables"><Paths><string>t\\*.txt</string></Paths></Action>'
            '<Action xsi:type="ModifyTobjFile"><FilePaths><string>m\\*.tobj</string></FilePaths>'
            "<ReplaceData><SearchFor>/x/</SearchFor><ReplaceWith>/y/{{Name}}/</ReplaceWith></ReplaceData>"
            "</Action>"
            '<Action xsi:type="ProcessArrayValues"><VariableName>L</VariableName>'
            "<IndexOffset>bad</IndexOffset>"
            '<BuildActions><Action xsi:type="AppendTextFile"><FileName>l.txt</FileName><TextLines>'
            "<string>{{ITEM}}{{INDEX:D3}}{{Yes}}{{F}}</string></TextLines></Action></BuildActions></Action>"
            '<Action xsi:type="ConditionalAction"><ExecuteIf>[Name]</ExecuteIf>'
            "<Comparison>Contains</Comparison>"
            "<ComparisonValue>bo</ComparisonValue><BuildActions>"
            '<Action xsi:type="GenerateTobjFile"><Content>ui\\i.dds</Content><FileName>ui/i.tobj</FileName>'
            "<TobjType>UIIcon</TobjType></Action></BuildActions></Action>"
            '<Action xsi:type="DeleteFile"><Paths><string>m2\\b.txt</string></Paths></Action>'
            '<Action xsi:type="DeleteDirectory"><Paths><string>x\\</string></Paths></Action>'
            "</BuildActions></Template>"
        )
        files = Engine(item, self.root, lambda t: None, {"x/old": b"o"}).run()
        self.assertEqual(files["t/b.txt"], b"box")
        self.assertEqual(formats.read_tobj_path(files["m/a.tobj"]), "/y/box/a.dds")
        self.assertEqual(files["l.txt"], b"p000Y1.50\r\nq001Y1.50\r\n")
        self.assertEqual(formats.read_tobj_path(files["ui/i.tobj"]), "/ui/i.dds")
        self.assertIn("m2/a.tobj", files)
        self.assertNotIn("m2/b.txt", files)
        self.assertNotIn("x/old", files)

    def test_conditions_and_errors(self):
        e = engine("", self.root)
        for cmp, val, want in (("Equals", "", True), ("NotEquals", "a", True), ("Empty", "", True)):
            a = parse(
                f"<A><ExecuteIf>[Missing]</ExecuteIf><Comparison>{cmp}</Comparison>"
                f"<ComparisonValues><string>{val}</string></ComparisonValues></A>"
            )
            self.assertEqual(e._cond(a), want)
        self.assertFalse(e._cond(parse("<A><ExecuteIf>X</ExecuteIf><Comparison>NotEmpty</Comparison></A>")))
        with self.assertRaises(TemplateError):
            e._cond(parse("<A><Comparison>Weird</Comparison></A>"))
        with self.assertRaises(TemplateError):
            engine('<Action xsi:type="Teleport"/>', self.root).run()
        with self.assertRaises(TemplateError):
            engine(
                '<Action xsi:type="CopyFile"><Paths><P><From>nope.txt</From><To>a</To></P></Paths></Action>',
                self.root,
            ).run()
        with self.assertRaises(TemplateError):
            engine(
                '<Action xsi:type="ImportImage"><Source></Source><Destination>a.dds</Destination></Action>',
                self.root,
            ).run()
        with self.assertRaises(TemplateError):
            engine(
                '<Action xsi:type="ModifyPmdMaterials"><FilePath>no.pmd</FilePath></Action>', self.root
            ).run()
        e = engine("", self.root, {"t.dds": TextureJob(self.root, "mask", None, "DXT5")})
        with self.assertRaises(TemplateError):
            e.read("t.dds")

    def test_extract_archive_and_import_image(self):
        src = self.root / "m.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("def/a.sii", "A")
        img = self.root / "i.png"
        Image.new("RGBA", (4, 4)).save(img)
        item = parse(
            '<Template xmlns:xsi="x"><Variables><Variable xsi:type="ScsImageSourceVariable"><Name>Icon</Name>'
            '<Validators><Validator xsi:type="ImageSizeValidator"><Width>8</Width><Height>4</Height>'
            "</Validator></Validators></Variable></Variables><BuildActions>"
            '<Action xsi:type="ExtractScsArchive"><Paths><P><Source>m.zip</Source><Target>\\x</Target></P>'
            "</Paths></Action>"
            '<Action xsi:type="ImportImage"><Source>{{Icon}}</Source><Destination>i.dds</Destination>'
            "<OutputFormat><Preset>ScsIconImage</Preset></OutputFormat></Action>"
            '<Action xsi:type="ImportImage"><Source>i.png</Source><Destination>j.dds</Destination></Action>'
            "</BuildActions></Template>"
        )
        e = Engine(item, self.root, lambda t: self.root / t if (self.root / t).is_file() else None)
        e.fallback_image = lambda raw: img
        files = e.run()
        self.assertEqual(files["x/def/a.sii"], b"A")
        self.assertEqual(files["i.dds"], TextureJob(img, "icon", (8, 4), "BGRA_SRGB"))
        self.assertEqual(files["j.dds"].purpose, "mask")
        e = engine(
            '<Action xsi:type="ExtractScsArchive"><Paths><P><Source>no.zip</Source></P></Paths></Action>',
            self.root,
        )
        with self.assertRaises(TemplateError):
            e.run()


# ====================================================================== builder (F5, F6, F11, F29)


def write_project(d: Path, vehicles: str, skin: str) -> None:
    (d / "vehicles").mkdir(parents=True)
    (d / "vehicles" / "v.xml").write_text(f"<vehicles>{vehicles}</vehicles>")
    (d / "project.xml").write_text(f'<project><mod package="pk" title="t"/>{skin}</project>')


class BuilderFixes(TempDir):
    def test_colliding_mask_folders_are_an_error(self):
        """F5: x.a_b and x_a.b share a mask folder; that must be reported, not overwritten."""
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(self.root / "red.png")
        Image.new("RGBA", (64, 64), (0, 0, 255, 255)).save(self.root / "blue.png")
        write_project(
            self.root,
            '<vehicle id="x.a_b"/><vehicle id="x_a.b"/>',
            '<skin id="s"><target vehicle="x.a_b" image="red.png"/><target vehicle="x_a.b" image="blue.png"/>'
            "</skin>",
        )
        _files, issues = plan(load_project(self.root))
        errors = [i for i in issues if i.level == "error"]
        # the .dds clashes (reported once); the .tobj files are byte-identical, so they don't
        self.assertEqual(len(errors), 1)
        self.assertIn("two different files", errors[0].message)
        self.assertTrue(errors[0].message.endswith("/x_a_b/s/default.dds"))

    def test_same_image_twice_is_fine_and_names_unchanged(self):
        Image.new("RGBA", (64, 64)).save(self.root / "a.png")
        write_project(
            self.root,
            '<vehicle id="scania.r"/><vehicle id="volvo.fh"/>',
            '<skin id="s"><target vehicle="scania.r" image="a.png"/>'
            '<target vehicle="volvo.fh" image="a.png"/>'
            "</skin>",
        )
        files, issues = plan(load_project(self.root))
        self.assertEqual([i for i in issues if i.level == "error"], [])
        self.assertIn("vehicle/truck/upgrade/paintjob/pk/scania_r/s/default.dds", {f.path for f in files})

    def test_plan_issues(self):
        Image.new("RGBA", (60, 30)).save(self.root / "npot.png")
        (self.root / "broken.png").write_bytes(b"no image")
        write_project(
            self.root,
            '<vehicle id="a.b"/><vehicle id="c.d"><template width="128" height="128"/></vehicle>',
            '<skin id="s"><target vehicle="a.b" image="npot.png"/><target vehicle="c.d" image="npot.png"/>'
            '</skin><skin id="t"><target vehicle="a.b" image="broken.png"/></skin>',
        )
        files, issues = plan(load_project(self.root))
        text = " ".join(i.message for i in issues)
        self.assertIn("resizing to 64x32", text)
        self.assertIn("will be resized", text)
        self.assertIn("cannot open image", text)
        jobs = {f.job.size for f in files if f.job and f.job.purpose == "mask"}
        self.assertEqual(jobs, {(64, 32), (128, 128)})
        p = model.Project(self.root, self.root / "project.xml")
        self.assertEqual(plan(p)[1][0].message, "missing <mod> element")

    def test_source_saved_during_build(self):
        """F6: a file touched while encoding must not crash the build with a KeyError."""
        src = self.root / "img.png"
        Image.new("RGBA", (8, 8)).save(src)
        real = bmod.encode

        def enc(job):
            data = real(job)
            os.utime(job.src, ns=(time.time_ns(), time.time_ns() + 10**9))
            return data

        with mock.patch.object(bmod, "encode", enc):
            r = Builder(None, workers=1).build_files(
                [OutFile("a.dds", job=TextureJob(src, "mask", None, "DXT5"))],
                self.root / "o.scs",
                "fast",
                self.root,
                log=QUIET,
            )
        self.assertTrue(r.ok, r.issues)

    def test_missing_source_is_an_issue(self):
        job = TextureJob(self.root / "gone.png", "mask", None, "DXT5")
        r = Builder(None, workers=1).build_files(
            [OutFile("a.dds", job=job)], self.root / "o.scs", "fast", self.root, log=QUIET
        )
        self.assertFalse(r.ok)
        self.assertIn("could not read", r.issues[0].message)

    def test_bad_image_reported_once_without_thread_fallback(self):
        """F29: a corrupt image is reported straight away, not re-encoded on threads first."""
        bad = self.root / "bad.png"
        bad.write_bytes(b"not a png")
        good = self.root / "good.png"
        Image.new("RGBA", (8, 8)).save(good)
        files = [
            OutFile("a.dds", job=TextureJob(bad, "mask", None, "DXT5")),
            OutFile("b.dds", job=TextureJob(good, "mask", None, "DXT5")),
        ]
        b = Builder(None, workers=2)
        try:
            with mock.patch.object(Builder, "_thread_map", side_effect=AssertionError("fallback")):
                r = b.build_files(files, self.root / "o.scs", "fast", self.root, log=QUIET)
        finally:
            b.close()
        self.assertFalse(r.ok)
        self.assertIn("could not convert an image", r.issues[0].message)

    def test_pool_unavailable_falls_back_to_threads(self):
        good = self.root / "good.png"
        Image.new("RGBA", (8, 8)).save(good)
        jobs = [TextureJob(good, "mask", None, "DXT5"), TextureJob(good, "copy", None, "RAW")]
        b = Builder(None, workers=2)
        with mock.patch.object(bmod, "ProcessPoolExecutor", side_effect=OSError("no processes")):
            out = b._pool_map(jobs)
        self.assertEqual(out[1], good.read_bytes())

    def test_worker_init_limits_threads(self):
        old = bcn.THREADS
        try:
            bmod._worker_init()
            self.assertEqual(bcn.THREADS, 1)
        finally:
            bcn.THREADS = old

    def test_cache_is_pruned_lru(self):
        """F11: the disk cache stays under its cap; the least recently used files go first."""
        cache = self.root / "cache"
        cache.mkdir()
        for i, name in enumerate(("old", "mid", "new")):
            (cache / name).write_bytes(bytes(100))
            os.utime(cache / name, ns=(10**18 + i * 10**9, 10**18 + i * 10**9))
        b = Builder(cache, workers=1, cache_max_bytes=250)
        self.assertEqual(b.prune_cache(keep={"old"}), 100)
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ["new", "old"])
        b.cache_max_bytes = 0
        b.prune_cache()
        self.assertEqual(list(cache.iterdir()), [])
        self.assertEqual(Builder(None).prune_cache(), 0)

    def test_build_uses_cache_and_prunes(self):
        cache = self.root / "cache"
        imgs = []
        for i in range(3):
            p = self.root / f"i{i}.png"
            Image.new("RGBA", (16, 16), (i * 50, 0, 0, 255)).save(p)
            imgs.append(p)
        b = Builder(cache, workers=1, cache_max_bytes=1)
        for p in imgs:
            r = b.build_files(
                [OutFile("a.dds", job=TextureJob(p, "mask", None, "DXT5"))],
                self.root / "o.zip",
                "small",
                self.root,
                log=QUIET,
            )
            self.assertTrue(r.ok)
        self.assertEqual(len(list(cache.iterdir())), 1)  # only the current build's texture is kept
        r = b.build_files(
            [OutFile("a.dds", job=TextureJob(imgs[2], "mask", None, "DXT5"))],
            self.root / "o.zip",
            "small",
            self.root,
            log=QUIET,
        )
        self.assertEqual((r.textures_cached, r.textures_encoded), (1, 0))
        b.clear_cache()
        self.assertFalse(cache.exists())

    def test_locked_output(self):
        with mock.patch.object(bmod, "write_zip", side_effect=PermissionError):
            r = Builder(None, workers=1).build_files(
                [OutFile("a", b"a")], self.root / "o.scs", "fast", self.root, log=QUIET
            )
        self.assertFalse(r.ok)
        self.assertIn("in use", r.issues[0].message)

    def test_build_project_errors(self):
        write_project(self.root, '<vehicle id="a.b"/>', '<skin id="BAD"/>')
        r = Builder(None, workers=1).build(load_project(self.root), log=QUIET)
        self.assertFalse(r.ok)
        (self.root / "broken.png").write_bytes(b"x")
        (self.root / "project.xml").write_text(
            '<project><mod package="pk"/><skin id="s"><target vehicle="a.b" image="broken.png"/></skin>'
            "</project>"
        )
        r = Builder(None, workers=1).build(load_project(self.root), log=QUIET)
        self.assertFalse(r.ok)

    def test_icon_overlay_and_jpg(self):
        src = self.root / "s.png"
        Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(src)
        ov = self.root / "o.png"
        Image.new("RGBA", (10, 10), (0, 0, 255, 255)).save(ov)
        dds = bmod.encode(TextureJob(src, "icon", (16, 8), "BGRA_SRGB", overlay=ov))
        self.assertTrue(formats.is_dds(dds))
        self.assertEqual(formats.dds_size(dds), (16, 8))
        jpg = bmod.encode(TextureJob(src, "modicon", (27, 16), "JPG"))
        self.assertEqual(Image.open(io.BytesIO(jpg)).size, (27, 16))
        ready = self.root / "r.dds"
        ready.write_bytes(dds)
        self.assertEqual(bmod.encode(TextureJob(ready, "mask", None, "DXT5")), dds)


# ====================================================================== model


class ModelLoading(TempDir):
    def test_colors(self):
        self.assertIs(model.srgb_to_linear, util.srgb_to_linear)
        self.assertEqual(parse_color("#fff"), (1.0, 1.0, 1.0))
        self.assertEqual(parse_color("(0.5, 0, 1)"), (0.5, 0.0, 1.0))
        for bad in ("nan,nan,nan", "inf,0,0", "2,0,0", "#12", "1,2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_color(bad)

    def test_many_issues(self):
        (self.root / "vehicles").mkdir()
        (self.root / "vehicles" / "a.xml").write_text(
            '<vehicles><vehicle id="BAD"/><vehicle id="a.b" kind="bus"/>'
            '<vehicle id="c.d" game="xx"><template width="x" height="2"/><slot id="Bad"/>'
            '<slot id="cab"><acc>a</acc></slot><slot id="cab"/><slot id="n"/></vehicle>'
            '<vehicle id="e.f"><slot id="m1" main="true"/><slot id="m2" main="true"/></vehicle>'
            '<vehicle id="c.d" game="ats"/></vehicles>'
        )
        (self.root / "vehicles" / "b.xml").write_text("<other/>")
        (self.root / "vehicles" / "c.xml").write_text("<vehicles>")
        Image.new("RGBA", (4, 4)).save(self.root / "i.png")
        (self.root / "i.xyz").write_bytes(b"")
        (self.root / "project.xml").write_text(
            '<project><mod package="X" game="psx" description="none.txt"/>'
            '<build compression="huge" texture_format="BC7" icon_size="big"/><vehicles dir="missing"/>'
            '<vehicles dir="vehicles"/>'
            '<skin id="s" mode="odd" base_color="#zz" foo="1" price="x" icon="noicon.png">'
            '<attr name="Bad"/><attr name="ok_key" value="1"/>'
            '<target vehicle="c.d" image="none.png"/><target image="i.png"/>'
            '<target vehicle="c.d" image="i.xyz"/><target vehicle="c.d" slot="zz" image="i.png"/>'
            '<target vehicle="q.q" image="i.png"/><target vehicle="c.d" image="i.png"/></skin>'
            '<skin id="s"/><skin id="BAD"/><skin id="empty"/></project>'
        )
        p = load_project(self.root)
        text = "\n".join(str(i) for i in p.issues)
        for needle in (
            "must be dot-separated",
            "kind='bus'",
            "not ets2/ats",
            "not a whole number",
            "slot id 'Bad'",
            "duplicate slot",
            "needs main=",
            "only one slot",
            "also defined",
            "expected <vehicle>",
            "XML syntax error",
            "package='X'",
            "must be ets2 or ats",
            "file not found",
            "compression must",
            "texture_format must",
            "icon_size must",
            "vehicles folder not found",
            "mode='odd'",
            "bad hex",
            "unknown attribute",
            "<attr> needs",
            "icon not found",
            "image not found",
            "<target> needs",
            "unusual image type",
            "has no slot",
            "unknown vehicle",
            "given twice",
            "duplicate skin id",
            "skin id 'BAD'",
            "no targets",
        ):
            self.assertIn(needle, text)
        self.assertEqual(p.compression, "fast")
        self.assertIn(self.root / "i.png", p.watched)

    def test_cross_checks(self):
        Image.new("RGBA", (4, 4)).save(self.root / "i.png")
        write_project(
            self.root,
            '<vehicle id="a.b" game="ats"><slot id="m" main="true"/>'
            '<slot id="c"><acc>x</acc></slot></vehicle>',
            '<skin id="s"><target vehicle="a.b" slot="c" image="i.png"/></skin>',
        )
        text = " ".join(i.message for i in load_project(self.root).issues)
        self.assertIn("needs an image for the main slot", text)
        self.assertIn("is for ats", text)
        self.assertEqual(load_project(self.root / "nothing").issues[0].level, "error")
        (self.root / "project.xml").write_text("<other/>")
        self.assertIn("root element", str(load_project(self.root).issues[0]))
        (self.root / "project.xml").write_text("<project/>")
        self.assertIn("missing <mod>", str(load_project(self.root / "project.xml").issues[0]))


# ====================================================================== caches (F16, F35)


class Caches(TempDir):
    def test_ms2data_xml_cache_is_bounded_and_refreshes(self):
        ms2data.clear_cache()
        f = self.root / "c.xml"
        f.write_text("<C><Value>a</Value></C>")
        self.assertEqual(ms2data._load_xml(f).get_text("Value"), "a")
        self.assertIs(ms2data._load_xml(f), ms2data._load_xml(f))
        f.write_text("<C><Value>bb</Value></C>")
        self.assertEqual(ms2data._load_xml(f).get_text("Value"), "bb")
        self.assertEqual(ms2data._parse_cached.cache_info().maxsize, ms2data._XML_CACHE_SIZE)
        f.write_text("<broken")
        self.assertIsNone(ms2data._load_xml(f))
        self.assertIsNone(ms2data._load_xml(self.root / "missing.xml"))
        self.assertIsNone(ms2data.cargo_preset(ms2data.Choice("x", "x")))
        c = ms2data.cargo_preset(ms2data.Choice("x", "x", path=self.root / "c2.xml"))
        self.assertIsNone(c)

    def test_ms2_folder_lists(self):
        ms2 = self.root / "MS2"
        comp = ms2 / "Data/ModData/SCS/Companies/ETS2"
        comp.mkdir(parents=True)
        (comp / "acme.xml").write_text("<C><Value>acme</Value><Text>Acme</Text></C>")
        cargo = ms2 / "Data/ModData/SCS/Cargo/ETS2"
        cargo.mkdir(parents=True)
        (cargo / "big_box.xml").write_text("<Cargo/>")
        (ms2 / "Data/ModProjects/ETS2/Templates/T").mkdir(parents=True)
        (ms2 / "Data/ModProjects/ETS2/Templates/T/template.xml").write_text(
            '<Template xmlns:xsi="x" xsi:type="Thing"><TemplateName>Thing</TemplateName></Template>'
        )
        (ms2 / "Data/ModProjects/ETS2/Templates/U").mkdir()
        self.assertEqual([c.text for c in ms2data.companies(ms2, "ets2")], ["Acme"])
        presets = ms2data.cargo_presets(ms2, "ets2")
        self.assertEqual(presets[0].text, "big box")
        self.assertEqual(ms2data.cargo_preset(presets[0]).tag, "Cargo")
        self.assertEqual(ms2data.template(ms2, "ets2", "Thing").name, "Thing")
        self.assertEqual(ms2data.templates(None, "ets2"), {})
        self.assertEqual(ms2data.companies(None, "ets2"), [])
        with mock.patch.dict(os.environ, {"PAINTBOOTH_MS2": str(ms2)}):
            self.assertEqual(ms2data.find(), ms2)
        self.assertEqual(ms2data.find({"ms2_dir": str(ms2)}, ["", str(ms2)]), ms2)
        hints = ms2data.project_hints(
            parse("<P><Variable><Name>Env.ApplicationDirectory</Name><Value>C:\\MS2</Value></Variable></P>")
        )
        self.assertEqual(hints, ["C:\\MS2"])
        self.assertEqual(ms2data.project_hints(None), [])

    def test_tplbuild_configure(self):
        ms2 = self.root / "MS2"
        (ms2 / "Data" / "ModProjects").mkdir(parents=True)
        proj = mock.Mock(root=None)
        try:
            tplbuild.configure({"ms2_dir": str(ms2)})
            self.assertEqual(tplbuild.ms2_dir(proj), ms2)
            live = {}
            tplbuild.configure(live)
            live["ms2_dir"] = str(ms2)  # kept by reference: later changes are seen
            self.assertEqual(tplbuild.ms2_dir(proj), ms2)
        finally:
            tplbuild.configure(None)
        tplbuild.SETTINGS = {"ms2_dir": str(ms2)}  # the old way still works
        try:
            self.assertEqual(tplbuild.ms2_dir(proj), ms2)
        finally:
            tplbuild.SETTINGS = {}

    def test_template_index_cache_is_bounded(self):
        templates.clear_cache()
        for i in range(templates._INDEX_CACHE_SIZE + 3):
            d = self.root / f"t{i}" / "Scania R"
            d.mkdir(parents=True)
            (d / "s.psd").write_bytes(b"x")
            self.assertEqual(len(templates.index("ets2", d.parent)), 1)
        self.assertEqual(len(templates._cache), templates._INDEX_CACHE_SIZE)
        self.assertEqual(templates.index("ets2", self.root / "missing"), [])
        self.assertEqual(templates.guess_game("American trucks"), "ats")
        self.assertIsNone(templates.guess_game("misc"))
        self.assertEqual(templates.safe_name(" a:b? "), "a_b_")
        self.assertEqual(templates.safe_name(""), "Untitled")


# ====================================================================== formats


class Formats(TempDir):
    def test_zip_round_trip_with_non_ascii_names(self):
        entries = [("def/ä.sii", pack_member(b"x" * 500, 9)), ("a.bin", pack_member(os.urandom(200), 9))]
        p = self.root / "m.scs"
        write_zip(p, entries)
        with zipfile.ZipFile(p) as z:
            self.assertEqual(z.read("def/ä.sii"), b"x" * 500)
            self.assertEqual(z.getinfo("a.bin").compress_type, 0)  # incompressible: stored
        self.assertEqual(pack_member(b"tiny", 9).method, 0)

    def test_dxt_decodes_close_to_source(self):
        img = Image.new("RGBA", (32, 32))
        for x in range(32):
            for y in range(32):
                img.putpixel((x, y), (x * 8, y * 8, 128, 255 if x < 20 else x * 4))
        for fmt in ("DXT5", "DXT1"):
            dds = formats.dds_bytes(img, fmt)
            back = Image.open(io.BytesIO(dds)).convert("RGBA")
            diff = max(
                abs(a - b)
                for pa, pb in zip(img.get_flattened_data(), back.get_flattened_data(), strict=True)
                for a, b in zip(pa[:3], pb[:3], strict=True)
            )
            self.assertLess(diff, 40, fmt)
        bgra = formats.dds_bytes(img, "BGRA")
        self.assertEqual(len(bgra), 128 + sum(4 * s * s for s in (32, 16, 8, 4, 2, 1)))
        with self.assertRaises(ValueError):
            formats.dds_bytes(img, "BC7")
        with self.assertRaises(ValueError):
            formats.read_tobj_path(b"\0" * 48)
        with self.assertRaises(ValueError):
            formats.tobj_bytes("relative/path.dds")

    def test_encoder_threads_and_odd_sizes(self):
        img = Image.new("RGBA", (6, 300), (10, 200, 30, 128))
        img.putpixel((0, 0), (0, 0, 0, 0))
        old = bcn.THREADS
        try:
            bcn.THREADS = 4
            a = bcn.encode_level(img, "DXT5")
            bcn.THREADS = 1
            self.assertEqual(bcn.encode_level(img, "DXT5"), a)
        finally:
            bcn.THREADS = old
        self.assertEqual(len(bcn.mip_chain(img, False)), 1)


# ====================================================================== HashFS reading (F14, F22)


def hashfs_v1(entries: dict[str, bytes], dirs: dict[str, str], compress: set[str]) -> bytes:
    """A HashFS v1 archive: files plus text directory listings."""
    items = [(k, v, False) for k, v in entries.items()] + [(k, v.encode(), True) for k, v in dirs.items()]
    data = bytearray(bytes(64))
    table = bytearray()
    for name, body, is_dir in items:
        stored = zlib.compress(body) if name in compress else body
        flags = (1 if is_dir else 0) | (2 if name in compress else 0)
        table += struct.pack(
            "<QQIIII", cityhash64(name.encode()), len(data), flags, 0, len(body), len(stored)
        )
        data += stored
    start = len(data)
    data += table
    struct.pack_into("<IHHIIQ", data, 0, hashfs.HASHFS_MAGIC, 1, 0, 0, len(items), start)
    return bytes(data)


def hashfs_v2(entries: dict[str, bytes], dirs: dict[str, list[str]], extra: int = 0) -> bytes:
    """A HashFS v2 archive: compressed tables, binary directory listings, 16-byte aligned data."""
    items = [(k, v, False) for k, v in entries.items()]
    for k, names in dirs.items():
        raw = [n.encode() for n in names]
        items.append((k, struct.pack("<I", len(raw)) + bytes(len(n) for n in raw) + b"".join(raw), True))
    data = bytearray(bytes(64))
    et, mt = bytearray(), bytearray()
    for i, (name, body, is_dir) in enumerate(items):
        while len(data) % 16:
            data += b"\0"
        block = len(data) // 16
        stored = zlib.compress(body)
        data += stored
        cs, s = len(stored), len(body)
        et += struct.pack("<QIHH", cityhash64(name.encode()), i * 5, 1, 0)
        mt += bytes([0, 0, 0, 129 if is_dir else 128])
        mt += struct.pack(
            "<BBBBBBBBII",
            cs & 255,
            cs >> 8 & 255,
            cs >> 16 & 255,
            (cs >> 24 & 15) | 0x10,
            s & 255,
            s >> 8 & 255,
            s >> 16 & 255,
            s >> 24 & 15,
            0,
            block,
        )
    for j in range(extra):  # entries no listing reveals (e.g. packed textures)
        et += struct.pack("<QIHH", 1000 + j, len(items) * 5 + j * 5, 1, 0)
        mt += bytes([0, 0, 0, 128]) + struct.pack("<BBBBBBBBII", 1, 0, 0, 0, 1, 0, 0, 0, 0, 0)
    et_c, mt_c = zlib.compress(bytes(et)), zlib.compress(bytes(mt))
    et_start = len(data)
    data += et_c
    mt_start = len(data)
    data += mt_c
    struct.pack_into(
        "<IHHIIIIIQQ",
        data,
        0,
        hashfs.HASHFS_MAGIC,
        2,
        0,
        0,
        len(items) + extra,
        len(et_c),
        0,
        len(mt_c),
        et_start,
        mt_start,
    )
    return bytes(data)


class HashFsReading(TempDir):
    def test_v1(self):
        p = self.root / "v1.scs"
        p.write_bytes(
            hashfs_v1(
                {"manifest.sii": b"M", "def/a.sii": b"A" * 300},
                {"": "*def\nmanifest.sii\n", "def": "a.sii\n"},
                {"def/a.sii"},
            )
        )
        with HashFs(p) as fs:
            self.assertEqual(fs.version, 1)
            self.assertEqual(fs.read("/def/a.sii"), b"A" * 300)
            self.assertIsNone(fs.read("def"))
            self.assertIsNone(fs.read("nope"))
            self.assertIsNone(fs.listdir("manifest.sii"))
            self.assertEqual(sorted(fs.walk()), ["def/a.sii", "manifest.sii"])
        self.assertTrue(fs.f.closed)
        self.assertEqual(dict(read_archive(p, [])), {"manifest.sii": b"M", "def/a.sii": b"A" * 300})

    def test_v2_and_unreadable_warning(self):
        p = self.root / "v2.scs"
        p.write_bytes(
            hashfs_v2({"def/a.sii": b"A", "m.sii": b"M"}, {"": ["/def", "m.sii"], "def": ["a.sii"]}, 1)
        )
        with HashFs(p) as fs:
            self.assertEqual(fs.listdir(""), (["def"], ["m.sii"]))
            self.assertEqual(fs.read("def/a.sii"), b"A")
        warnings = []
        self.assertEqual(dict(read_archive(p, warnings)), {"def/a.sii": b"A", "m.sii": b"M"})
        self.assertEqual(len(warnings), 1)

    def test_salt_entry_limit_and_empty(self):
        p = self.root / "e.scs"
        p.write_bytes(hashfs_v2({"x": b"X"}, {}))
        with HashFs(p) as fs:
            fs.salt = 7
            self.assertEqual(fs._hash("a"), cityhash64(b"7a"))
            e = next(iter(fs.entries.values()))
            with self.assertRaises(hashfs.HashFsError):
                fs._read(e, limit=0)
        with self.assertRaises(TemplateError):  # no listing: nothing readable
            list(read_archive(p, []))

    def test_cityhash_lengths(self):
        # stable values across every length branch (computed once, guards against edits)
        seen = {cityhash64(bytes(range(n % 256)) * 1) for n in (0, 3, 8, 16, 24, 40, 64, 65, 200)}
        self.assertEqual(len(seen), 9)


if __name__ == "__main__":
    unittest.main()
