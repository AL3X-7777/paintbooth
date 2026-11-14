"""Tests for the Windows installer helpers, the installer build script and the release notes.

Covers ``installer/winsetup.py`` (with an in-memory stand-in for ``winreg`` so the registry code
runs on any OS), ``installer/build_installer.py`` (hash-pinned wheels and the SKYPAYLD payload
trailer, including the Authenticode-signed layout), ``.github/release_notes.py``, and, when the
``ziglang`` package is installed, the C payload extractor of ``installer/setup.c`` built as its
Linux test harness.
"""

import _isolation  # noqa: F401, I001  (must be first: points HOME at a temporary folder)

import hashlib
import importlib.util
import io
import logging
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PureWindowsPath
from types import ModuleType
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "installer"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


winsetup = _load("winsetup", INSTALLER / "winsetup.py")
build_installer = _load("build_installer", INSTALLER / "build_installer.py")
release_notes = _load("release_notes", REPO / ".github" / "release_notes.py")


# ---------------------------------------------------------------- a fake winreg


class FakeWinreg:
    """Just enough of ``winreg`` for winsetup: keys are case-insensitive paths in a dict."""

    HKEY_CURRENT_USER = 1
    KEY_WRITE = KEY_ALL_ACCESS = KEY_SET_VALUE = KEY_READ = 0
    REG_SZ, REG_DWORD = 1, 4

    def __init__(self):
        self.keys: dict[str, dict[str, tuple[object, int]]] = {}

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    @staticmethod
    def _norm(path):
        return path.lower()

    def CreateKeyEx(self, root, path, res=0, access=0):
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault(self._norm("\\".join(parts[:i])), {})
        return self._Key(self._norm(path))

    def OpenKey(self, root, path, res=0, access=0):
        if self._norm(path) not in self.keys:
            raise FileNotFoundError(path)
        return self._Key(self._norm(path))

    def SetValueEx(self, key, name, res, kind, val):
        self.keys[key.path][name] = (val, kind)

    def QueryValueEx(self, key, name):
        try:
            val, kind = self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None
        return val, kind

    def DeleteValue(self, key, name):
        try:
            del self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def EnumKey(self, key, i):
        prefix = key.path + "\\"
        subs = sorted({k[len(prefix) :].split("\\")[0] for k in self.keys if k.startswith(prefix)})
        if i >= len(subs):
            raise OSError("no more keys")
        return subs[i]

    def DeleteKey(self, root, path):
        self.keys.pop(self._norm(path))

    def value(self, path, name=""):
        return self.keys.get(self._norm(path), {}).get(name, (None,))[0]


# ---------------------------------------------------------------- winsetup: pure helpers


class SamePathTest(unittest.TestCase):
    def test_case_slashes_and_trailing_separator(self):
        self.assertTrue(winsetup.same_path(r"C:\Programs\Paintbooth\\", "c:/programs/PAINTBOOTH"))

    def test_different(self):
        self.assertFalse(winsetup.same_path(r"C:\Programs\Paintbooth", r"C:\Programs\Paintbooth 2"))

    def test_drive_root_keeps_its_backslash(self):
        self.assertTrue(winsetup.same_path("C:\\", "c:\\"))
        self.assertFalse(winsetup.same_path("C:\\", "C:"))


class RegistryValuesTest(unittest.TestCase):
    def test_uninstall_values(self):
        root = Path("/x/Paintbooth")
        vals = winsetup.uninstall_values(
            root, root / "app", root / "Paintbooth.exe", root / "runtime" / "pythonw.exe",
            root / "app" / "paintbooth.ico", "1.2.3", 4096,
        )  # fmt: skip
        self.assertEqual(vals["DisplayName"], "Paintbooth")
        self.assertEqual(vals["DisplayVersion"], "1.2.3")
        self.assertEqual(vals["InstallLocation"], str(root))
        self.assertEqual(
            vals["UninstallString"],
            f'"{root / "runtime" / "pythonw.exe"}" "{root / "app" / "winsetup.py"}" uninstall',
        )
        # ints become REG_DWORD, so they must really be ints
        for k in ("NoModify", "NoRepair", "EstimatedSize"):
            self.assertIsInstance(vals[k], int)
        self.assertEqual(vals["EstimatedSize"], 4096)

    def test_open_command_quotes_both(self):
        self.assertEqual(
            winsetup.open_command(Path("C:/P b/Paintbooth.exe")), f'"{Path("C:/P b/Paintbooth.exe")}" "%1"'
        )


class ShortcutDecisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.start = self.tmp / "start.lnk"
        self.desk = self.tmp / "desk.lnk"

    def test_start_menu_only_by_default(self):
        self.assertEqual(winsetup.shortcut_targets(self.start, self.desk, False), [self.start])

    def test_desktop_when_asked(self):
        self.assertEqual(winsetup.shortcut_targets(self.start, self.desk, True), [self.start, self.desk])

    def test_existing_desktop_icon_is_kept_on_update(self):
        self.desk.write_bytes(b"")
        self.assertEqual(winsetup.shortcut_targets(self.start, self.desk, False), [self.start, self.desk])

    def test_powershell_script_quotes_apostrophes(self):
        script = winsetup.shortcut_script(
            [Path("/a/O'Brien.lnk")], Path("/e.exe"), Path("/r"), Path("/i.ico")
        )
        self.assertIn("CreateShortcut('" + str(Path("/a/O''Brien.lnk")) + "')", script)
        self.assertEqual(winsetup.ps_quote("it's"), "'it''s'")


class OldLocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.new = self.tmp / "Programs" / "Paintbooth"

    def test_default_location_and_recorded_location(self):
        rec = self.tmp / "Elsewhere" / "Skinyard"
        self.assertEqual(
            winsetup.old_install_folders(self.new, rec), [self.tmp / "Programs" / "Skinyard", rec]
        )

    def test_no_duplicates_and_never_the_new_folder(self):
        same = Path(str(self.tmp / "Programs" / "SKINYARD") + "\\")
        self.assertEqual(winsetup.old_install_folders(self.new, same), [self.tmp / "Programs" / "Skinyard"])
        self.assertEqual(
            winsetup.old_install_folders(self.new, self.new), [self.tmp / "Programs" / "Skinyard"]
        )

    def test_is_program_folder_needs_runtime(self):
        self.assertFalse(winsetup.is_program_folder(self.tmp / "missing"))
        (self.tmp / "user").mkdir()
        self.assertFalse(winsetup.is_program_folder(self.tmp / "user"))
        (self.tmp / "user" / "runtime").mkdir()
        self.assertTrue(winsetup.is_program_folder(self.tmp / "user"))

    def test_remove_program_files_keeps_user_files(self):
        root = self.tmp / "PB"
        for d in ("runtime/Lib", "app/paintbooth"):
            (root / d).mkdir(parents=True)
        (root / "Paintbooth.exe").write_bytes(b"MZ")
        (root / "notes.txt").write_text("mine")
        winsetup.remove_program_files(root)
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["notes.txt"])

    def test_remove_program_files_removes_empty_folder(self):
        root = self.tmp / "PB2"
        (root / "runtime").mkdir(parents=True)
        (root / "Paintbooth.exe").write_bytes(b"MZ")
        winsetup.remove_program_files(root)
        self.assertFalse(root.exists())


class UninstallScriptTest(unittest.TestCase):
    def test_percent_in_the_folder_is_escaped(self):
        root = PureWindowsPath(r"D:\100%sure\%PATH%\Paintbooth")
        script = winsetup.uninstall_script(root)
        self.assertIn(r'"D:\100%%sure\%%PATH%%\Paintbooth\runtime"', script)
        self.assertNotIn("100%s", script)  # no single % that cmd would try to expand
        self.assertTrue(script.startswith("@echo off\r\nchcp 65001"))

    def test_only_program_files_are_deleted(self):
        script = winsetup.uninstall_script(PureWindowsPath(r"C:\P\Paintbooth"))
        self.assertEqual(script.count("rmdir /s /q"), 2)  # runtime and app only
        self.assertIn('\r\nrmdir "C:\\P\\Paintbooth" 2>nul', script)  # the folder: only if empty
        self.assertIn('del "%~f0"', script)  # the script removes itself

    def test_script_is_started_by_bare_name_from_temp(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(winsetup.subprocess, "Popen") as popen:
            with mock.patch.object(winsetup.tempfile, "gettempdir", return_value=t):
                winsetup.start_uninstall_script(PureWindowsPath(r"C:\P\Paintbooth"))
            (bat,) = list(Path(t).glob("paintbooth_uninstall_*.bat"))
            args, kw = popen.call_args
            self.assertEqual(args[0], ["cmd", "/d", "/c", bat.name])
            self.assertEqual(kw["cwd"], t)
            self.assertIn(b"C:\\P\\Paintbooth\\app", bat.read_bytes())


# ---------------------------------------------------------------- winsetup: Windows parts, mocked


class FakeRegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "Programs" / "Paintbooth"
        (self.root / "app").mkdir(parents=True)
        (self.root / "app" / "x.py").write_text("x" * 3000)
        for name, kw in (
            ("_winreg", {"return_value": self.reg}),
            ("_notify_assoc", {}),
            ("ROOT", {"new": self.root}),
            ("APP", {"new": self.root / "app"}),
            ("EXE", {"new": self.root / "Paintbooth.exe"}),
            ("_shortcut_paths", {"return_value": [self.tmp / "Paintbooth.lnk"]}),
        ):
            p = mock.patch.object(winsetup, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_register_and_unregister(self):
        winsetup.register()
        self.assertEqual(self.reg.value(winsetup.UNINSTALL_KEY, "InstallLocation"), str(self.root))
        self.assertEqual(self.reg.value(r"Software\Classes\.pb"), winsetup.PROGID)
        self.assertEqual(self.reg.value(r"Software\Classes\.ms2\OpenWithProgids", winsetup.PROGID), "")
        self.assertIsNone(self.reg.value(r"Software\Classes\.ms2"))  # .ms2 is not taken over
        winsetup.unregister()
        self.assertNotIn(winsetup.UNINSTALL_KEY.lower(), self.reg.keys)
        self.assertNotIn(r"software\classes\.pb", self.reg.keys)
        self.assertNotIn(winsetup.PROGID, self.reg.keys.get(r"software\classes\.ms2\openwithprogids", {}))

    def test_pb_owned_by_another_program_is_left_alone(self):
        self.reg.CreateKeyEx(1, r"Software\Classes\.pb")
        self.reg.keys[r"software\classes\.pb"][""] = ("Other.App", 1)
        winsetup.register()
        winsetup.unregister()
        self.assertEqual(self.reg.value(r"Software\Classes\.pb"), "Other.App")

    def test_remove_old_install_uses_recorded_location(self):
        old = self.tmp / "Custom" / "Skinyard"
        (old / "runtime").mkdir(parents=True)
        (self.tmp / "Skinyard.lnk").write_bytes(b"")
        winsetup._set(1, winsetup.OLD_UNINSTALL_KEY, {"InstallLocation": str(old)})
        winsetup._set(1, r"Software\Classes\.ms2\OpenWithProgids", {winsetup.OLD_PROGID: ""})
        winsetup.remove_old_install()
        self.assertFalse(old.exists())
        self.assertFalse((self.tmp / "Skinyard.lnk").exists())
        self.assertNotIn(winsetup.OLD_UNINSTALL_KEY.lower(), self.reg.keys)
        self.assertEqual(self.reg.keys[r"software\classes\.ms2\openwithprogids"], {})

    def test_update_from_the_paint_booth_name(self):
        """0.10 and earlier were called "Paint booth": its shortcuts, launcher and folder go away."""
        (self.tmp / "Paint booth.lnk").write_bytes(b"")
        self.assertTrue(winsetup.had_desktop_icon(self.tmp / "Paintbooth.lnk"))
        self.assertFalse(winsetup.had_desktop_icon(self.tmp / "Elsewhere" / "Paintbooth.lnk"))
        winsetup._set(1, r"Software\Classes\Applications\Paint booth.exe\shell", {"": "x"})
        winsetup.remove_old_install()
        self.assertFalse((self.tmp / "Paint booth.lnk").exists())
        self.assertNotIn(r"software\classes\applications\paint booth.exe\shell", self.reg.keys)
        # the previous install folder: program files go, the user's own file stays
        prev = self.tmp / "Programs" / "Paint booth"
        (prev / "runtime").mkdir(parents=True)
        (prev / "app").mkdir()
        (prev / "Paint booth.exe").write_bytes(b"")
        (prev / "mine.txt").write_text("user file")
        winsetup.remove_program_files(prev)
        self.assertEqual([p.name for p in prev.iterdir()], ["mine.txt"])

    def test_remove_old_install_never_deletes_a_non_program_folder(self):
        old = self.tmp / "Custom" / "Skinyard"
        old.mkdir(parents=True)
        (old / "keep.txt").write_text("user file")
        winsetup._set(1, winsetup.OLD_UNINSTALL_KEY, {"InstallLocation": str(old)})
        winsetup.remove_old_install()
        self.assertTrue((old / "keep.txt").exists())

    def test_install_logs_failed_steps_and_carries_on(self):
        with (
            mock.patch.object(winsetup, "remove_old_install", side_effect=PermissionError("locked")),
            mock.patch.object(winsetup, "_ensure_documents"),
            mock.patch.object(winsetup, "make_shortcuts") as shortcuts,
            mock.patch.object(winsetup, "register") as register,
            self.assertLogs(winsetup.log, logging.ERROR) as logs,
        ):
            self.assertEqual(winsetup.install(), 0)
        shortcuts.assert_called_once()
        register.assert_called_once()
        self.assertIn("remove the old Skinyard install", "\n".join(logs.output))

    def test_previous_location_is_removed_when_moving(self):
        prev = self.tmp / "Old place" / "Paintbooth"
        (prev / "runtime").mkdir(parents=True)
        (prev / "mine.pb").write_text("x")
        winsetup._set(1, winsetup.UNINSTALL_KEY, {"InstallLocation": str(prev)})
        winsetup._remove_previous_location()
        self.assertEqual([p.name for p in prev.iterdir()], ["mine.pb"])

    def test_previous_location_same_folder_is_kept(self):
        (self.root / "runtime").mkdir()
        winsetup._set(1, winsetup.UNINSTALL_KEY, {"InstallLocation": str(self.root).upper() + "\\"})
        winsetup._remove_previous_location()
        self.assertTrue((self.root / "runtime").is_dir())

    def test_setup_logging_writes_to_temp(self):
        path = winsetup.setup_logging(str(self.tmp))
        try:
            winsetup.log.error("hello")
        finally:
            for h in list(winsetup.log.handlers):
                if isinstance(h, logging.FileHandler):
                    h.close()
                    winsetup.log.removeHandler(h)
        self.assertEqual(path.name, "PaintBoothSetup.log")
        self.assertIn("hello", path.read_text(encoding="utf-8"))

    def test_main_unknown_command(self):
        with mock.patch.object(winsetup, "setup_logging"):
            self.assertEqual(winsetup.main(["winsetup.py", "frobnicate"]), 2)


# ---------------------------------------------------------------- release notes


class ReleaseNotesTest(unittest.TestCase):
    CHANGELOG = "# Changelog\n\n## 0.2.0 - 2026-01-02\n\n* New thing\n\n## 0.1.0\n\n* First\n"

    def test_section(self):
        self.assertEqual(release_notes.changelog_section(self.CHANGELOG, "0.2.0"), "* New thing")
        self.assertEqual(release_notes.changelog_section(self.CHANGELOG, "0.1.0"), "* First")
        self.assertEqual(release_notes.changelog_section(self.CHANGELOG, "0.1"), "")  # whole version only

    def test_notes(self):
        notes = release_notes.release_notes("0.2.0", self.CHANGELOG)
        self.assertTrue(notes.startswith("* New thing\n\n**Install:** download *Paintbooth Setup 0.2.0.exe*"))
        self.assertIn("CODE_SIGNING.md", notes)

    def test_missing_section_falls_back_to_title(self):
        self.assertTrue(release_notes.release_notes("9.9.9", "").startswith("Paintbooth 9.9.9\n"))


# ---------------------------------------------------------------- build_installer: wheels


class RequirementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_offline_requirements_cover_bundled_packages(self):
        reqs = build_installer.read_requirements(build_installer.OFFLINE_REQUIREMENTS)
        for name in build_installer.BUNDLED_PACKAGES:
            self.assertIn(name, reqs)
            self.assertTrue(all(len(h) == 64 for h in reqs[name][1]))

    def test_local_wheels_match_their_hashes(self):
        reqs = build_installer.read_requirements(build_installer.OFFLINE_REQUIREMENTS)
        wheels = sorted(build_installer.WHEELS.glob("*.whl"))
        if not wheels:
            self.skipTest("wheels/ not fetched in this checkout")
        for w in wheels:
            build_installer.verify_wheel(w, reqs)

    def test_unpinned_is_rejected(self):
        f = self.tmp / "r.txt"
        f.write_text("pillow>=1 --hash=sha256:" + "0" * 64 + "\n")
        with self.assertRaises(build_installer.BuildError):
            build_installer.read_requirements(f)
        f.write_text("pillow==1.0\n")
        with self.assertRaises(build_installer.BuildError):
            build_installer.read_requirements(f)

    def test_tampered_wheel_is_rejected(self):
        w = self.tmp / "demo-1.0-py3-none-any.whl"
        w.write_bytes(b"real")
        good = hashlib.sha256(b"real").hexdigest()
        f = self.tmp / "r.txt"
        f.write_text(f"# c\ndemo==1.0 \\\n    --hash=sha256:{good}\n")
        reqs = build_installer.read_requirements(f)
        build_installer.verify_wheel(w, reqs)
        w.write_bytes(b"evil")
        with self.assertRaisesRegex(build_installer.BuildError, "SHA-256"):
            build_installer.verify_wheel(w, reqs)
        with self.assertRaisesRegex(build_installer.BuildError, "missing"):
            build_installer.verify_wheel(self.tmp / "demo-1.0-x.whl", reqs)
        with self.assertRaisesRegex(build_installer.BuildError, "not pinned"):
            build_installer.verify_wheel(w.rename(self.tmp / "demo-2.0-py3-none-any.whl"), reqs)


# ---------------------------------------------------------------- build_installer: payload trailer


def _zip(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, data in names.items():
            z.writestr(n, data)
    return buf.getvalue()


def _fake_pe(size: int = 0x200) -> bytearray:
    """A minimal PE32+ header: enough for the trailer locator (e_lfanew, signature, magic)."""
    pe = bytearray(size)
    pe[0:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 0x80)
    pe[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", pe, 0x80 + 24, 0x20B)
    return pe


def _sign(exe: bytes) -> bytes:
    """Imitate Authenticode: pad to 8 bytes, append a certificate table, point the PE at it."""
    signed = bytearray(exe + b"\0" * (-len(exe) % 8))
    cert = len(signed)
    signed += b"CERT" * 64
    struct.pack_into("<II", signed, 0x80 + 24 + 112 + 4 * 8, cert, 256)
    return bytes(signed)


class PayloadTrailerTest(unittest.TestCase):
    def test_round_trip_unsigned(self):
        stub, payload = bytes(_fake_pe()), _zip({"a.txt": b"x"})
        exe = build_installer.build_setup(stub, payload)
        self.assertEqual(exe[-16:-8], b"SKYPAYLD")
        off, size = build_installer.find_payload(exe)
        self.assertEqual(exe[off : off + size], payload)

    def test_round_trip_signed(self):
        stub, payload = bytes(_fake_pe(0x201)), _zip({"a.txt": b"x" * 7})  # odd sizes: padding needed
        signed = _sign(build_installer.build_setup(stub, payload))
        off, size = build_installer.find_payload(signed)
        self.assertEqual(signed[off : off + size], payload)

    def test_no_trailer(self):
        with self.assertRaises(build_installer.BuildError):
            build_installer.find_payload(bytes(_fake_pe()))

    def test_bad_offset(self):
        exe = bytes(_fake_pe()) + b"SKYPAYLD" + struct.pack("<Q", 10**9)
        with self.assertRaises(build_installer.BuildError):
            build_installer.find_payload(exe)


@unittest.skipUnless(importlib.util.find_spec("ziglang"), "needs the ziglang package (C compiler)")
class SetupHarnessTest(unittest.TestCase):
    """Build setup.c's portable core with -DSKY_TEST and run it on crafted payloads."""

    tmp: Path
    harness: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "harness"
        miniz = INSTALLER / "miniz"
        subprocess.run(
            [sys.executable, "-m", "ziglang", "cc", "-O1", "-DSKY_TEST", "-DMINIZ_NO_ZLIB_COMPATIBLE_NAMES",
             f"-I{miniz}", str(INSTALLER / "setup.c"), str(miniz / "miniz.c"), "-o", str(cls.harness)],
            check=True, capture_output=True, timeout=600,
        )  # fmt: skip

    def run_payload(self, exe: bytes) -> tuple[int, Path]:
        out = Path(tempfile.mkdtemp(dir=self.tmp)) / "out"
        (out.parent / "setup.exe").write_bytes(exe)
        r = subprocess.run(
            [str(self.harness), str(out.parent / "setup.exe"), str(out)], check=False, capture_output=True
        )
        return r.returncode, out

    def test_extracts_unsigned_and_signed(self):
        payload = _zip({"app/a.txt": b"hello", "Paintbooth.exe": b"MZ"})
        for exe in (
            build_installer.build_setup(bytes(_fake_pe()), payload),
            _sign(build_installer.build_setup(bytes(_fake_pe(0x203)), payload)),
        ):
            rc, out = self.run_payload(exe)
            self.assertEqual(rc, 0)
            self.assertEqual((out / "app" / "a.txt").read_bytes(), b"hello")

    def test_unsafe_names_are_refused(self):
        for name in ("../evil.txt", "app/../../evil.txt", "/abs.txt", "C:evil.txt", "a\\..\\..\\evil.txt"):
            with self.subTest(name=name):
                rc, out = self.run_payload(build_installer.build_setup(bytes(_fake_pe()), _zip({name: b"x"})))
                self.assertEqual(rc, 8)
                self.assertFalse((out.parent / "evil.txt").exists())

    def test_no_trailer(self):
        rc, _ = self.run_payload(bytes(_fake_pe()))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
