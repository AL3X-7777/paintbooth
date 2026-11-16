"""Security regressions for the core build engine: every path-traversal vector (review F1, F2).

Each test plants a "secret" or a target just outside the folder the code is meant to stay in,
and checks that nothing was read from or written to it.
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from paintbooth import appdirs, hashfs, ms2data, ms2tpl, templates
from paintbooth.builder import Builder, OutFile
from paintbooth.ms2tpl import Engine, TemplateError, read_archive
from paintbooth.safepath import UnsafePathError, clean_member, has_traversal, parts_of, safe_join
from paintbooth.xmldom import parse

QUIET = lambda *a: None  # noqa: E731


def engine(actions: str, tdir: Path, resolve=lambda t: None) -> Engine:
    xml = (
        '<Template xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><BuildActions>'
        + actions
        + "</BuildActions></Template>"
    )
    return Engine(parse(xml), tdir, resolve)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class SafePath(TempDir):
    BAD = (
        "/etc/passwd",
        "\\Windows\\x.bat",
        "../x",
        "a/../../x",
        "a\\..\\..\\x",
        "C:/x",
        "c:x",
        "\\\\host\\share\\x.exe",
        "//host/share/x",
        "a/file.txt:stream",
    )

    def test_clean_member_rejects_every_vector(self):
        for name in self.BAD:
            with self.subTest(name=name), self.assertRaises(UnsafePathError):
                clean_member(name)

    def test_clean_member_normalises_good_names(self):
        self.assertEqual(clean_member("a\\b//./c.txt"), "a/b/c.txt")
        self.assertEqual(clean_member("/def/x.sii", allow_root=True), "def/x.sii")
        self.assertEqual(clean_member(""), "")
        with self.assertRaises(UnsafePathError):
            clean_member("/../x", allow_root=True)

    def test_helpers(self):
        self.assertEqual(parts_of("./a\\\\b/"), ["a", "b"])
        self.assertTrue(has_traversal("a/../b"))
        self.assertTrue(has_traversal("D:/b"))
        self.assertFalse(has_traversal("/a/b"))

    def test_safe_join(self):
        self.assertEqual(safe_join(self.root, "a/b"), self.root / "a" / "b")
        self.assertEqual(safe_join(self.root, ""), self.root)
        for name in self.BAD:
            with self.subTest(name=name), self.assertRaises(UnsafePathError):
                safe_join(self.root, name)

    def test_safe_join_refuses_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        inside = self.root / "inside"
        inside.mkdir()
        (inside / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UnsafePathError):
            safe_join(inside, "link/x.txt")


class TemplatePackZipSlip(TempDir):
    """F1: a downloaded template pack must not write outside Templates/<game>."""

    def test_zip_members_cannot_escape(self):
        outside = self.root / "escaped.txt"
        z = self.root / "pack.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Scania/a.png", b"x")
            zf.writestr(str(outside), b"absolute")  # /tmp/.../escaped.txt
            zf.writestr("..\\..\\..\\escaped2.txt", b"dotdot")
            zf.writestr("C:/escaped3.txt", b"drive")
            zf.writestr("\\\\host\\share\\escaped4.txt", b"unc")
        templates.import_pack(z, "ets2")
        self.assertFalse(outside.exists())
        dest = appdirs.templates_dir("ets2")
        self.assertTrue((dest / "Scania" / "a.png").is_file())
        for f in dest.rglob("*"):
            self.assertTrue(f.resolve().is_relative_to(dest.resolve()), f)
        for name in ("escaped2.txt", "escaped3.txt"):
            self.assertFalse((self.root / name).exists())
            self.assertFalse((dest.parent.parent / name).exists())

    def test_folder_import_skips_symlinks(self):
        src = self.root / "pack"
        (src / "Volvo").mkdir(parents=True)
        (src / "Volvo" / "fh.psd").write_bytes(b"psd")
        secret = self.root / "secret.txt"
        secret.write_text("secret")
        (src / "Volvo" / "leak.png").symlink_to(secret)
        seen = []
        n = templates.import_pack(src, "ats", progress=lambda *a: seen.append(a))
        dest = appdirs.templates_dir("ats")
        self.assertEqual(n, 1)
        self.assertFalse((dest / "Volvo" / "leak.png").exists())
        self.assertEqual(seen[-1], (1, 1, ""))
        # importing again copies nothing
        self.assertEqual(templates.import_pack(src, "ats"), 0)


class CiPath(TempDir):
    """F2: template paths can't leave the template folder."""

    def setUp(self):
        super().setUp()
        self.base = self.root / "tpl"
        (self.base / "Model").mkdir(parents=True)
        (self.base / "Model" / "Box.pmd").write_bytes(b"x")
        (self.root / "secret.txt").write_text("secret")

    def test_case_insensitive(self):
        self.assertEqual(ms2data.ci_path(self.base, "model\\BOX.pmd"), self.base / "Model" / "Box.pmd")
        self.assertEqual(ms2data.ci_path(self.base, "\\Model\\"), self.base / "Model")
        self.assertIsNone(ms2data.ci_path(self.base, "model/missing"))

    def test_refuses_traversal(self):
        for rel in ("../secret.txt", "Model/../../secret.txt", "C:/secret.txt"):
            with self.subTest(rel=rel):
                self.assertIsNone(ms2data.ci_path(self.base, rel))

    def test_refuses_symlink_out(self):
        (self.base / "link").symlink_to(self.root / "secret.txt")
        self.assertIsNone(ms2data.ci_path(self.base, "link"))


class EnginePaths(TempDir):
    """F2: template build actions can neither read outside the template nor write outside the mod."""

    def setUp(self):
        super().setUp()
        self.tdir = self.root / "MS2/Data/ModProjects/ETS2/Templates/X"
        self.tdir.mkdir(parents=True)
        (self.tdir / "a.txt").write_text("A")
        (self.root / "secret.txt").write_text("user secret")

    def test_copy_from_outside_is_refused(self):
        e = engine(
            '<Action xsi:type="CopyFile"><Paths><P><From>../../../../../../secret.txt</From>'
            "<To>def/leak.txt</To></P></Paths></Action>",
            self.tdir,
        )
        with self.assertRaises(TemplateError):
            e.run()
        self.assertNotIn("def/leak.txt", e.files)

    def test_write_outside_the_mod_is_refused(self):
        for name in ("../../outside.txt", "C:\\outside.txt", "a/../../outside.txt"):
            with self.subTest(name=name):
                e = engine(
                    f'<Action xsi:type="WriteTextFile"><FileName>{name}</FileName>'
                    "<TextLines><string>hi</string></TextLines></Action>",
                    self.tdir,
                )
                with self.assertRaises(TemplateError):
                    e.run()

    def test_dotdot_inside_the_mod_is_resolved(self):
        self.assertEqual(Engine.norm("\\a\\b\\..\\c.txt"), "a/c.txt")
        self.assertEqual(Engine.norm("a/b/../"), "a/")
        self.assertEqual(Engine.norm(".//a/./b"), "a/./b")  # unchanged, as before
        e = engine(
            '<Action xsi:type="CopyFile"><Paths><P><From>sub/../a.txt</From><To>def/a.txt</To></P>'
            "</Paths></Action>",
            self.tdir,
        )
        self.assertEqual(e.run()["def/a.txt"], self.tdir / "a.txt")


class BuildOutputPaths(TempDir):
    """F2: build_files never writes outside its output, and never puts '..' into an archive."""

    def test_folder_output(self):
        out = self.root / "build" / "mod"
        files = [OutFile("def/ok.sii", b"ok"), OutFile("../../outside.txt", b"x")]
        r = Builder(None, workers=1).build_files(files, out, "fast", self.root, log=QUIET)
        self.assertFalse(r.ok)
        self.assertIn("unsafe", r.issues[0].message)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_archive_output(self):
        out = self.root / "mod.scs"
        for bad in ("../x.sii", "/abs.sii", "C:/x.sii"):
            with self.subTest(bad=bad):
                r = Builder(None, workers=1).build_files(
                    [OutFile(bad, b"x")], out, "fast", self.root, log=QUIET
                )
                self.assertFalse(r.ok)
                self.assertFalse(out.exists())

    def test_folder_output_refuses_foreign_folder(self):
        out = self.root / "mine"
        out.mkdir()
        (out / "keep.txt").write_text("user file")
        r = Builder(None, workers=1).build_files([OutFile("a.sii", b"a")], out, "fast", self.root, log=QUIET)
        self.assertFalse(r.ok)
        self.assertTrue((out / "keep.txt").exists())
        # a previous build is replaced
        out2 = self.root / "out2"
        b = Builder(None, workers=1)
        self.assertTrue(b.build_files([OutFile("a.sii", b"a")], out2, "fast", self.root, log=QUIET).ok)
        r = b.build_files([OutFile("b/c.sii", b"c")], out2, "fast", self.root, log=QUIET)
        self.assertTrue(r.ok)
        self.assertFalse((out2 / "a.sii").exists())
        self.assertEqual((out2 / "b" / "c.sii").read_bytes(), b"c")


class ArchiveMembers(TempDir):
    """F2: another mod's archive can't inject '..' paths, and its size is limited."""

    def test_unsafe_members_skipped(self):
        src = self.root / "other.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("def/a.sii", "A")
            z.writestr("../../evil.sii", "E")
            z.writestr("/abs/b.sii", "B")
            z.writestr("C:/evil2.sii", "E")
        warnings: list[str] = []
        got = dict(read_archive(src, warnings))
        self.assertEqual(got, {"def/a.sii": b"A", "abs/b.sii": b"B"})
        self.assertEqual(len(warnings), 2)

    def test_member_size_limit(self):
        src = self.root / "bomb.zip"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.bin", bytes(4096))
        with mock.patch.object(ms2tpl, "MAX_ENTRY_BYTES", 1000), self.assertRaises(TemplateError):
            list(read_archive(src, []))
        with mock.patch.object(ms2tpl, "MAX_ARCHIVE_BYTES", 1000), self.assertRaises(TemplateError):
            list(read_archive(src, []))

    def test_unreadable_archive_is_a_template_error(self):
        src = self.root / "junk.scs"
        src.write_bytes(b"not an archive at all" * 10)
        with self.assertRaises(TemplateError):
            list(read_archive(src, []))

    def test_inflate_limit(self):
        data = zlib.compress(bytes(10000))
        self.assertEqual(len(hashfs._inflate(data, 10000)), 10000)
        with self.assertRaises(hashfs.HashFsError):
            hashfs._inflate(data, 100)
        with self.assertRaises(hashfs.HashFsError):
            hashfs._inflate(data[:-6], 20000)  # truncated stream


class HashFsHandles(TempDir):
    """F22: a rejected archive does not leave its file open."""

    def test_bad_magic_closes_file(self):
        p = self.root / "x.scs"
        p.write_bytes(struct.pack("<IHH", 0x12345678, 1, 0) + bytes(60))
        opened = []
        real_open = open

        def spy(*a, **k):
            f = real_open(*a, **k)
            opened.append(f)
            return f

        with mock.patch("builtins.open", spy), self.assertRaises(hashfs.HashFsError):
            hashfs.HashFs(p)
        self.assertTrue(opened and all(f.closed for f in opened))

    def test_unsupported_version_and_short_file(self):
        p = self.root / "v9.scs"
        p.write_bytes(struct.pack("<IHH", hashfs.HASHFS_MAGIC, 9, 0) + bytes(60))
        with self.assertRaises(hashfs.HashFsError):
            hashfs.HashFs(p)
        p.write_bytes(b"SCS#")
        with self.assertRaises(hashfs.HashFsError):
            hashfs.HashFs(p)

    def test_damaged_tables(self):
        p = self.root / "bad.scs"
        head = struct.pack("<IHHIIIIIQQ", hashfs.HASHFS_MAGIC, 2, 0, 0, 1, 10, 1, 10, 64, 74)
        p.write_bytes(head.ljust(64, b"\0") + b"garbage!!!" * 2)
        with self.assertRaises(hashfs.HashFsError):
            hashfs.HashFs(p)


class ReviewTwoFixes(unittest.TestCase):
    """Regression tests for the second independent review (R1-R16)."""

    def test_backtracking_regex_is_refused(self):
        from paintbooth.ms2tpl import TemplateError, backtracking_risk, compile_dotnet

        for bad in ("(a+)+$", "(a*)*b", r"(\w+\s?){2,}x"):
            self.assertTrue(backtracking_risk(bad), bad)
            with self.assertRaises(TemplateError):
                compile_dotnet(bad)
        for fine in (
            r"(?:\*|\d+)\.(?:\*|\d+)\.(?:\*|\d+)",
            r"^[a-z0-9_]{1,12}$",
            r"(ab)+",
            r"a+b*",
            r"\(a+\)+",
        ):
            self.assertFalse(backtracking_risk(fine), fine)
            compile_dotnet(fine)

    def test_next_version_never_downgrades(self):
        from paintbooth.ms2build import next_version

        self.assertEqual(next_version("1.04", None), "1.04")
        self.assertEqual(next_version("1.04", "1.04"), "1.05")
        self.assertEqual(next_version("1.99", "1.99"), "2.00")
        self.assertEqual(next_version("", None), "1.00")
        self.assertEqual(next_version("beta", "beta"), "1.00")
        self.assertEqual(next_version("1.5", None), "1.5")
        self.assertEqual(next_version("1.5", "1.5"), "1.6")
        self.assertEqual(next_version("2.10.1", "2.10.1"), "2.10.2")
        self.assertEqual(next_version("1.09b", "1.09b"), "1.10b")

    def test_device_names_and_trailing_dots_are_unsafe(self):
        from paintbooth.safepath import UnsafePathError, clean_member

        for bad in ("aux.txt", "def/NUL", "a./b", "a /b", "x/com1.sii"):
            with self.assertRaises(UnsafePathError, msg=bad):
                clean_member(bad)
        self.assertEqual(clean_member("def/console.sii"), "def/console.sii")
        self.assertEqual(clean_member("vehicle/truck/auxiliary.dds"), "vehicle/truck/auxiliary.dds")

    def test_update_link_must_be_the_projects_own(self):
        import http.client
        import io
        import json
        from unittest import mock

        from paintbooth import updates

        def reply(d):
            return mock.patch.object(
                updates.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(d).encode())
            )

        with reply({"tag_name": "v9.0.0", "html_url": "file://evil/x.exe"}):
            self.assertEqual(updates.latest_release(), ("9.0.0", updates.RELEASES_URL))
        good = updates.RELEASES_URL + "/tag/v9.0.0"
        with reply({"tag_name": "v9.0.0", "html_url": good}):
            self.assertEqual(updates.latest_release(), ("9.0.0", good))
        with mock.patch.object(
            updates.urllib.request, "urlopen", side_effect=http.client.IncompleteRead(b"")
        ):
            self.assertIsNone(updates.latest_release())
        # no release published yet: the version on the main branch is compared, linking the repo
        init = io.BytesIO(b'"""x"""\n__version__ = "9.1.0"\n')
        with (
            mock.patch.object(updates, "latest_release", return_value=None),
            mock.patch.object(updates.urllib.request, "urlopen", return_value=init),
        ):
            self.assertEqual(updates.check("0.1.0"), ("9.1.0", updates.REPO_URL))
        with (
            mock.patch.object(updates, "latest_release", return_value=None),
            mock.patch.object(updates.urllib.request, "urlopen", side_effect=OSError("offline")),
        ):
            self.assertIsNone(updates.check("0.1.0"))

    def test_cache_folder_problems_do_not_fail_the_build(self):
        import tempfile
        from pathlib import Path

        from paintbooth.builder import Builder

        with tempfile.TemporaryDirectory() as t:
            blocker = Path(t) / "file"
            blocker.write_text("x")
            b = Builder(blocker / "cache")  # a folder that can never be created
            try:
                b._store("k", b"data")
                self.assertEqual(b._cached("k"), b"data")  # still served from memory
                b.memory.clear()
                self.assertIsNone(b._cached("k"))
            finally:
                b.close()

    def test_failed_save_leaves_no_temp_file_and_keeps_the_path(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from paintbooth.ms2 import Ms2Project

        with tempfile.TemporaryDirectory() as t:
            p = Ms2Project.new("ets2")
            target = Path(t) / "a.pb"
            with (
                mock.patch.object(Path, "replace", side_effect=PermissionError("locked")),
                self.assertRaises(OSError),
            ):
                p.save(target)
            self.assertEqual(list(Path(t).iterdir()), [])
            self.assertIsNone(p.path)


if __name__ == "__main__":
    unittest.main()
