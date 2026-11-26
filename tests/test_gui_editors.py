"""Headless tests for the item editors and the SCS tool windows (group C2).

The widget tests need an X display (on CI: ``xvfb-run python -m unittest``) and a Python built with
Tk; without either they are skipped. The pure helpers (link check, TOBJ rebuild) need no display,
but they live in GUI modules that import tkinter, so they are skipped on a Python without Tk.

The editors run against :class:`FakeApp`, a stand-in for ``gui.main.App`` that has exactly the
attributes the editors use, so these tests don't depend on the main window's internals.
"""

import _isolation  # noqa: I001  (must come first: isolates the user folders)

import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
except ImportError:  # Python built without Tk
    tk = None  # type: ignore[assignment]

from paintbooth import vehlib
from paintbooth.formats import read_tobj_path, tobj_bytes
from paintbooth.ms2 import AdvancedTruckSkin, Ms2Project, TemplateItem
from paintbooth.xmldom import parse

DATA = Path(__file__).parent / "data"


def _display_ok() -> bool:
    if tk is None or (not os.environ.get("DISPLAY") and os.name != "nt"):
        return False
    try:
        r = tk.Tk()
    except tk.TclError:
        return False
    r.destroy()
    return True


HAVE_DISPLAY = _display_ok()
needs_display = unittest.skipUnless(HAVE_DISPLAY, "needs Tk and a display (run under xvfb-run)")
needs_tk = unittest.skipIf(tk is None, "tkinter not available")

# A small MS2 template: a string, an integer, an image, an image list and an info tab whose links
# include a web link and two that must never be opened (a UNC path and a javascript: URL).
TEMPLATE = r"""<?xml version="1.0" encoding="utf-8"?>
<Template xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Billboard">
  <BuildActions />
  <InfoTab>
    <InfoBox xsi:type="LinkBox">
      <Content>Your artwork on the roadside billboards.</Content>
      <Links>
        <Link><Text>Tutorial video</Text><Uri>https://www.youtube.com/watch?v=x</Uri></Link>
        <Link><Text>Evil share</Text><Uri>\\host\share\x.exe</Uri></Link>
        <Link><Text>Script</Text><Uri>javascript:alert(1)</Uri></Link>
      </Links>
    </InfoBox>
  </InfoTab>
  <TemplateName>Billboard</TemplateName>
  <Variables>
    <Variable xsi:type="StringVariable">
      <ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>InternalName</Name>
      <Text>Internal Name</Text>
    </Variable>
    <Variable xsi:type="IntegerVariable">
      <ChildVariables /><Group>basic</Group><IsEnabled>true</IsEnabled><Name>Price</Name><Text>Price</Text>
      <Value>0</Value>
    </Variable>
    <Variable xsi:type="ScsImageSourceVariable">
      <ChildVariables /><Group>icon</Group><IsEnabled>true</IsEnabled><Name>Icon</Name><Text>Icon</Text>
      <AllowedExtensions><string>png</string></AllowedExtensions>
    </Variable>
    <Variable xsi:type="ScsImageSourceArrayVariable">
      <ChildVariables /><Group>images</Group><IsEnabled>true</IsEnabled><Name>Images</Name><Text>Images</Text>
      <Validators><Validator xsi:type="ItemCountValidator"><MaxCount>3</MaxCount></Validator></Validators>
    </Variable>
  </Variables>
</Template>"""


class FakeApp:
    """The parts of ``gui.main.App`` that the editors and tool windows use."""

    def __init__(self, root: "tk.Tk", project: Ms2Project, folder: Path):
        from paintbooth.gui.icons import Icons
        from paintbooth.gui.widgets import ThumbCache, UiQueue

        self.root = root
        self.project = project
        self.folder = folder
        self.settings: dict = {}
        self.ui = UiQueue(root)
        self.thumbs = ThumbCache(root, self.ui)
        self.icons = Icons()
        self.library_dir = folder / "Vehicles"
        self.library_dir.mkdir(exist_ok=True)
        self.library, self.library_errors = vehlib.load_library(self.library_dir)
        self.vehicle_manager = None
        self.changes: list[dict] = []
        self.statuses: list[str] = []

    # project / UI
    def changed(self, label: bool = False, structure: bool = False) -> None:
        self.changes.append({"label": label, "structure": structure})

    def revalidate_soon(self) -> None:
        pass

    def show_problems(self) -> None:
        pass

    def status(self, text: str) -> None:
        self.statuses.append(text)

    def project_dir(self) -> Path:
        return self.folder

    def image_dir(self) -> str:
        return str(self.folder)

    def remember_image_dir(self, d: Path) -> None:
        self.settings["last_image_dir"] = str(d)

    def default_mod_icon(self) -> Path:
        return self.folder / "default_mod.png"

    def default_skin_icon(self) -> Path:
        return self.folder / "default_skin.png"

    def installed_dlcs(self) -> list:
        return []

    def next_export_version(self) -> str:
        return "1.00"

    # vehicles
    def vehicle_catalog(self) -> dict:
        return {k: (v, "library") for k, v in self.library.items()}

    def catalog_key(self, v: "vehlib.Vehicle | None") -> "str | None":
        return v.id if v is not None else None

    def reload_library(self) -> list:
        self.library, self.library_errors = vehlib.load_library(self.library_dir)
        return self.library_errors

    def scan_dialog(self, folder: bool = True) -> None:
        pass

    def install_library(self) -> None:
        pass

    def open_vehicle_manager(self, select: "str | None" = None) -> None:
        pass

    # hidden accessory categories: the real App's code, without the skin editor
    def save_settings(self) -> bool:
        return True

    def refresh_hidden_groups(self) -> None:
        if self.vehicle_manager is not None:
            self.vehicle_manager.refresh_groups()

    def hidden_groups(self, vehicle_id: str) -> set:
        from paintbooth.gui.main import App

        return App.hidden_groups(self, vehicle_id)  # type: ignore[arg-type]

    def set_hidden_groups(self, vehicle_id: str, groups) -> None:
        from paintbooth.gui.main import App

        App.set_hidden_groups(self, vehicle_id, groups)  # type: ignore[arg-type]

    @property
    def search_hidden_groups(self) -> bool:
        return bool(self.settings.get("search_hidden_groups", False))

    def set_search_hidden_groups(self, on: bool) -> None:
        from paintbooth.gui.main import App

        App.set_search_hidden_groups(self, on)  # type: ignore[arg-type]

    def open_tutorials(self, topic: "str | None" = None) -> None:
        pass


class _Tk:
    """The module's one Tk interpreter: Tk images (the shared icon cache) die with their root."""

    root: "tk.Tk | None" = None


def setUpModule():
    if HAVE_DISPLAY:
        from paintbooth.gui import icons

        _Tk.root = tk.Tk()
        _Tk.root.withdraw()
        icons._shared.clear()  # icons made for another test module's (destroyed) root


def tearDownModule():
    if _Tk.root is not None:
        from paintbooth.gui import icons

        icons._shared.clear()
        _Tk.root.destroy()
        _Tk.root = None


class TkCase(unittest.TestCase):
    """A hidden window per test (under the module's Tk root), plus a temporary project folder."""

    def setUp(self):
        self.root = tk.Toplevel(_Tk.root)
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory(dir=_isolation.TEMP_HOME)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        from paintbooth.gui import colorpick

        colorpick.configure(None)
        self.root.update_idletasks()
        self.root.destroy()
        self.tmp.cleanup()

    def pump(self) -> None:
        """Run pending Tk events and idle tasks."""
        self.root.update_idletasks()
        self.root.update()

    def app(self, project: "Ms2Project | None" = None) -> FakeApp:
        return FakeApp(self.root, project or Ms2Project.new("ets2"), self.dir)


# ---------------------------------------------------------------------------------- F4: link schemes
@needs_tk
class LinkSchemeTest(unittest.TestCase):
    """F4: only absolute http(s) links from project/template files may be opened."""

    def test_is_web_url(self):
        from paintbooth.gui.tpleditor import is_web_url

        for ok in ("https://www.youtube.com/watch?v=x", "http://forum.scssoft.com/x", " HTTPS://A.B/ "):
            self.assertTrue(is_web_url(ok), ok)
        for bad in (
            r"\\host\share\x.exe",
            r"C:\Users\me\payload.lnk",
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "ms-settings:",
            "https:/no-host",
            "http://[::1",  # urlsplit raises ValueError
            "",
        ):
            self.assertFalse(is_web_url(bad), bad)

    def test_open_web_url_refuses_others(self):
        from paintbooth.gui import tpleditor

        with mock.patch.object(tpleditor.webbrowser, "open") as op:
            self.assertFalse(tpleditor.open_web_url(r"\\host\share\x.exe"))
            op.assert_not_called()
            self.assertTrue(tpleditor.open_web_url("https://example.com/a"))
            op.assert_called_once_with("https://example.com/a")


# ---------------------------------------------------------------------------------- F30: TOBJ
def _tobj_with(settings: bytes, path: str = "/vehicle/a.dds", tail: bytes = b"") -> bytes:
    """A .tobj with custom header bytes 20-39 and optional extra data after the first path."""
    p = path.encode()
    return struct.pack("<I", 0x70B10A01) + bytes(16) + settings + struct.pack("<II", len(p), 0) + p + tail


@needs_tk
class TobjRebuildTest(unittest.TestCase):
    """F30: the TOBJ editor keeps header settings it doesn't know instead of replacing them."""

    def test_unknown_header_is_kept(self):
        from paintbooth.gui.tools import rebuild_tobj, tobj_profile_of

        custom = bytes.fromhex("01 00 02 00 02 00 03 03 03 00 00 00 00 00 00 00 00 01 00 00")
        custom = custom[:5] + b"\x07" + custom[6:]  # an address mode Paintbooth has no profile for
        orig = _tobj_with(custom, tail=b"\x05\x00\x00\x00\x00\x00\x00\x00/x/b")
        self.assertIsNone(tobj_profile_of(orig))
        out = rebuild_tobj(orig, "/vehicle/truck/new_path.dds", None)
        self.assertEqual(out[20:40], custom)
        self.assertEqual(read_tobj_path(out), "/vehicle/truck/new_path.dds")
        self.assertTrue(out.endswith(b"/x/b"))  # a second path (cube map) survives

    def test_known_profile_round_trip(self):
        from paintbooth.gui.tools import rebuild_tobj, tobj_profile_of

        for prof in ("skin", "ui", "game"):
            data = tobj_bytes("/vehicle/x.dds", profile=prof)
            self.assertEqual(tobj_profile_of(data), prof)
            # unchanged path and profile give the same bytes as before
            self.assertEqual(rebuild_tobj(data, "/vehicle/x.dds", prof), data)
            self.assertEqual(rebuild_tobj(data, "/vehicle/x.dds", None), data)

    def test_choosing_a_type_replaces_the_settings(self):
        from paintbooth.gui.tools import rebuild_tobj

        orig = _tobj_with(bytes(range(20)))
        out = rebuild_tobj(orig, "/vehicle/a.dds", "ui")
        self.assertEqual(out, tobj_bytes("/vehicle/a.dds", profile="ui"))

    def test_new_file_and_errors(self):
        from paintbooth.gui.tools import rebuild_tobj

        self.assertEqual(rebuild_tobj(None, "/v/a.dds", "skin"), tobj_bytes("/v/a.dds"))
        with self.assertRaises(ValueError):
            rebuild_tobj(None, "/v/a.dds", None)
        with self.assertRaises(UnicodeEncodeError):
            rebuild_tobj(None, "/v/é.dds", "skin")
        with self.assertRaises(struct.error):
            rebuild_tobj(b"\x01\x0a\xb1\x70", "/v/a.dds", None)


@needs_display
class TobjEditorTest(TkCase):
    """F30: the TOBJ window keeps unknown settings and reports failed saves."""

    def test_round_trip_keeps_unknown_header(self):
        from paintbooth.gui.tools import TobjEditor

        custom = bytes(range(1, 21))
        src = self.dir / "a.tobj"
        src.write_bytes(_tobj_with(custom))
        ed = TobjEditor(self.app())
        self.assertTrue(ed.load(src))
        self.assertEqual(ed.cb.get(), TobjEditor.KEEP)
        ed.path.set("/vehicle/truck/b.dds")
        with mock.patch("paintbooth.gui.tools.messagebox") as mb:
            self.assertTrue(ed.save())
        mb.showerror.assert_not_called()
        data = src.read_bytes()
        self.assertEqual(data[20:40], custom)
        self.assertEqual(read_tobj_path(data), "/vehicle/truck/b.dds")
        # picking a known type writes that type's settings
        ed.cb.current(1)
        self.assertTrue(ed.save())
        self.assertEqual(src.read_bytes(), tobj_bytes("/vehicle/truck/b.dds", profile="ui"))

    def test_failed_save_shows_error(self):
        from paintbooth.gui.tools import TobjEditor

        ed = TobjEditor(self.app())
        ed.path.set("/vehicle/a.dds")
        ed.file.set(str(self.dir / "missing folder" / "a.tobj"))
        with mock.patch("paintbooth.gui.tools.messagebox") as mb:
            self.assertFalse(ed.save())
        mb.showerror.assert_called_once()
        self.assertEqual(ed.msg.cget("text"), "Not saved")

    def test_non_ascii_path_shows_error(self):
        from paintbooth.gui.tools import TobjEditor

        ed = TobjEditor(self.app())
        ed.path.set("/vehicle/ü.dds")
        ed.file.set(str(self.dir / "a.tobj"))
        with mock.patch("paintbooth.gui.tools.messagebox") as mb:
            self.assertFalse(ed.save())
        self.assertIn("ASCII", mb.showerror.call_args[0][1])
        self.assertFalse((self.dir / "a.tobj").exists())

    def test_unreadable_file(self):
        from paintbooth.gui.tools import TobjEditor

        bad = self.dir / "bad.tobj"
        bad.write_bytes(b"nope")
        ed = TobjEditor(self.app())
        with mock.patch("paintbooth.gui.tools.messagebox") as mb:
            self.assertFalse(ed.load(bad))
        mb.showerror.assert_called_once()


@needs_display
class VehicleManagerTest(TkCase):
    """F30: a failed save of a vehicle XML shows an error and keeps the edits."""

    def setUp(self):
        super().setUp()
        self.fa = self.app()
        (self.fa.library_dir / DATA.joinpath("ETS2.koegel.cargo.xml").name).write_bytes(
            DATA.joinpath("ETS2.koegel.cargo.xml").read_bytes()
        )
        self.fa.reload_library()

    def _open(self):
        from paintbooth.gui.tools import VehicleManager

        vm = VehicleManager(self.fa)
        self.fa.vehicle_manager = vm
        v = next(iter(self.fa.library.values()))
        self.assertTrue(vm.open_file(Path(v.source), v.line))
        return vm

    def test_failed_save_reports_error(self):
        vm = self._open()
        vm.editor.text.insert("end", "\n<!-- edit -->")
        self.assertTrue(vm.dirty())
        with (
            mock.patch("paintbooth.gui.tools.vehlib.write_text", side_effect=PermissionError("locked")),
            mock.patch("paintbooth.gui.tools.messagebox") as mb,
            self.assertLogs("paintbooth.gui.tools", "WARNING"),
        ):
            self.assertFalse(vm.save())
        mb.showerror.assert_called_once()
        self.assertTrue(vm.dirty())  # nothing pretends to be saved
        self.assertIn("Not saved", vm.msg.cget("text"))

    def test_save_writes_and_reloads(self):
        vm = self._open()
        vm.editor.text.insert("end", "\n<!-- edit -->")
        with mock.patch("paintbooth.gui.tools.messagebox") as mb:
            self.assertTrue(vm.save())
        mb.showerror.assert_not_called()
        self.assertFalse(vm.dirty())
        self.assertIn("<!-- edit -->", vm.current.read_text(encoding=vm.encoding))
        self.assertTrue(vm.msg.cget("text").startswith("Saved"))


class HiddenGroupsTest(VehicleManagerTest):
    """Accessory categories hidden in the Vehicles window, without touching the vehicle file."""

    test_failed_save_reports_error = None  # inherited for setUp/_open only
    test_save_writes_and_reloads = None

    def test_hide_show_and_search_hidden(self):
        from paintbooth.gui.widgets import CheckList

        vm = self._open()
        key, v = next(iter(self.fa.library.items()))
        before = Path(v.source).read_bytes()
        vm.tv.selection_set(f"v::{key}")
        self.pump()
        rows = vm.groups_tv.get_children()
        self.assertEqual(len(rows), 9)
        # click "Axles": hidden, stored in the settings by vehicle id
        vm.groups_tv.see("g::Axles")
        self.pump()
        y = vm.groups_tv.bbox("g::Axles")[1] + 2
        vm._group_click(mock.Mock(y=y))
        self.assertEqual(self.fa.hidden_groups(v.id), {"Axles"})
        self.assertIn("hidden", vm.groups_tv.item("g::Axles", "text"))
        self.assertEqual(Path(v.source).read_bytes(), before)  # the vehicle is not changed

        check = CheckList(self.root, show_values=True)
        opts = [(a.value, a.name, a.group) for a in v.accessories]
        check.set_items(opts, ["axles.3_paint"])  # a ticked entry of a hidden category stays listed
        check.set_hidden(self.fa.hidden_groups(v.id), False)
        shown = check._visible_values()
        self.assertIn("axles.3_paint", shown)
        self.assertNotIn("axles.2_paint", shown)
        check.filter.set("axles.2_paint")
        self.assertEqual(check._visible_values(), [])  # not found while searching is off
        check.search_hidden.set(True)
        check._search_hidden_changed()
        self.assertIn("axles.2_paint", check._visible_values())

        # the option is shared with the Vehicles window, and "Show all" brings everything back
        self.fa.set_search_hidden_groups(True)
        self.assertTrue(vm.search_hidden.get())
        vm._show_all_groups()
        self.assertEqual(self.fa.hidden_groups(v.id), set())
        self.assertNotIn("hidden", vm.groups_tv.item("g::Axles", "text"))


# ---------------------------------------------------------------------------------- editors
@needs_display
class ManifestEditorTest(TkCase):
    def test_edit_fields_change_project(self):
        from paintbooth.gui.editors import ManifestEditor

        fa = self.app()
        ed = ManifestEditor(self.root, fa)
        ed.load(fa.project.manifest)
        self.assertEqual(fa.changes, [])  # loading is not an edit
        ed.name.set("My skins")
        ed.author.set("AL3X")
        self.assertEqual(fa.project.manifest.vars.value("display_name"), "My skins")
        self.assertEqual(fa.project.manifest.vars.value("author"), "AL3X")
        self.assertEqual(len(fa.changes), 2)
        ed.desc.insert("1.0", "Hello")
        self.pump()
        self.assertEqual(fa.project.manifest.vars.value("description"), "Hello")

    def test_compatible_versions_default_empty(self):
        """The user's rule: compatible_versions stays empty unless typed."""
        from paintbooth.gui.editors import ManifestEditor

        fa = self.app()
        ed = ManifestEditor(self.root, fa)
        ed.load(fa.project.manifest)
        self.assertEqual(ed.compat.get("1.0", "end-1c"), "")
        self.assertEqual(fa.project.manifest.vars.strings("compatible_versions"), [])

    def test_icon_lookup_is_debounced(self):
        """F12: typing an icon path stores it at once but looks it up once, after a pause."""
        from paintbooth.gui import editors

        fa = self.app()
        ed = editors.ManifestEditor(self.root, fa)
        ed.load(fa.project.manifest)
        with mock.patch.object(editors, "resolve", return_value=None) as res:
            for i in range(1, 6):
                ed.icon.set("C:/icons/abcde"[: 10 + i])
            self.assertEqual(fa.project.manifest.vars.value("icon"), "C:/icons/abcde")
            res.assert_not_called()
            self.assertTrue(ed._icon_soon.pending)
            ed._icon_soon.flush()
            self.assertEqual(res.call_count, 1)


@needs_display
class CoachOptionsTest(TkCase):
    def test_bus_bodies_get_a_windows_and_trim_option(self):
        from paintbooth.gui.editors import TruckSkinEditor

        bus = vehlib.Vehicle("a.coach", "ets2", "A", "Coach", vehicle_type="Buses")
        bus.variants.append(vehlib.Variant("c1.a.coach.cabin", "12 m"))
        ids = [o[0] for o in TruckSkinEditor._image_options("CabVariantSkin", bus)]
        self.assertEqual(ids, ["c1.a.coach.cabin", "c1.a.coach.cabin+films"])
        truck = vehlib.Vehicle("t.truck", "ets2", "T", "Truck")
        truck.variants.append(vehlib.Variant("c.t.truck.cabin", "Cab"))
        self.assertEqual(len(TruckSkinEditor._image_options("CabVariantSkin", truck)), 1)

    def test_notice_has_an_ok_button(self):
        from paintbooth.gui.widgets import Notice

        n = Notice(self.root)
        n.show("Exported", "")
        self.assertIsNotNone(n.frame)
        n.ok.invoke()
        self.assertIsNone(n.frame)


@needs_display
class UpdateDialogTest(TkCase):
    def test_dont_tell_me_again_turns_the_check_off(self):
        from paintbooth.gui.main import App

        fake = mock.Mock()
        fake.root, fake.settings = self.root, {"check_updates": True}
        with mock.patch("webbrowser.open") as web:
            App._update_dialog(fake, "9.0.0", "https://github.com/AL3X-7777/paintbooth")
            win = fake.update_dialog
            self.pump()
            boxes = [w for w in _walk(win) if w.winfo_class() == "TCheckbutton"]
            boxes[0].invoke()  # "Don't tell me again"
            later = [w for w in _walk(win) if w.winfo_class() == "TButton" and w.cget("text") == "Later"]
            later[0].invoke()
        self.assertFalse(fake.settings["check_updates"])
        fake.save_settings.assert_called_once()
        web.assert_not_called()


def _walk(w):
    for c in w.winfo_children():
        yield c
        yield from _walk(c)


@needs_display
class TruckSkinEditorTest(TkCase):
    def setUp(self):
        super().setUp()
        self.fa = self.app()
        (self.fa.library_dir / "koegel.xml").write_bytes(DATA.joinpath("ETS2.koegel.cargo.xml").read_bytes())
        self.fa.reload_library()
        i = self.fa.project.add_like("AdvancedTruckSkin", None)
        self.item = self.fa.project.items[i]
        self.assertIsInstance(self.item, AdvancedTruckSkin)
        self.item.set_vehicle(next(iter(self.fa.library.values())))

    def editor(self):
        from paintbooth.gui.editors import TruckSkinEditor

        ed = TruckSkinEditor(self.root, self.fa)
        ed.load(self.item)
        self.fa.changes.clear()
        return ed

    def test_edit_fields_change_project(self):
        ed = self.editor()
        ed.v["SkinName"].set("Blue")
        self.assertEqual(self.item.get("SkinName"), "Blue")
        self.assertEqual(self.fa.changes[-1]["label"], True)
        ed.v["SkinPrice"].set("12,0")
        self.assertEqual(self.item.get("SkinPrice"), "12")
        ed.v["FlakeDensity"].set("2.50")
        self.assertEqual(self.item.get("FlakeDensity"), "2.5")
        for junk in ("abc", "nan", "inf"):  # not stored, and never an exception
            ed.v["SkinPrice"].set(junk)
            self.assertEqual(self.item.get("SkinPrice"), "12")

    def test_add_image_and_debounced_preview(self):
        from paintbooth.gui import editors

        ed = self.editor()
        ed.add_image("CabVariantSkin")
        entries = self.item.entries("CabVariantSkin")
        self.assertEqual(len(entries), 1)
        veh = self.item.vehicle()
        self.assertEqual(entries[0].suitable, [x.id for x in veh.variants])  # first image: all bodies
        block = ed.blocks["CabVariantSkin"][0]
        with mock.patch.object(editors, "resolve", return_value=None) as res:
            for part in ("s", "sk", "ski", "skin.png"):
                block.path.set(part)
            self.assertEqual(self.item.entries("CabVariantSkin")[0].image, "skin.png")
            res.assert_not_called()
            block._preview_soon.flush()
            res.assert_called_once()
        self.assertEqual(block.info_lbl.cget("text"), "File not found")

    def test_no_dead_layout_code(self):
        """F18: the manifest editor has no Notebook layout switch any more."""
        from paintbooth.gui import editors

        self.assertFalse(hasattr(editors.ManifestEditor, "_layout_changed"))


@needs_display
class TemplateEditorTest(TkCase):
    def setUp(self):
        super().setUp()
        p = Ms2Project.new("ets2")
        i = p.add_template(parse(TEMPLATE.encode()), None)
        self.item = p.items[i]
        self.assertIsInstance(self.item, TemplateItem)
        self.fa = self.app(p)

    def editor(self):
        from paintbooth.gui.tpleditor import TemplateEditor

        ed = TemplateEditor(self.root, self.fa)
        ed.load(self.item)
        return ed

    @staticmethod
    def _labels(w):
        out = []
        for c in w.winfo_children():
            if c.winfo_class() == "TLabel":
                out.append(c)
            out += TemplateEditorTest._labels(c)
        return out

    def test_edit_field_changes_project(self):
        ed = self.editor()
        self.assertIn("InternalName", ed.marks)
        entry = next(w for w in ed.rows if w[0].get_text("Name") == "InternalName")[1][
            1
        ]  # (variable, [label, widget, mark], lookup)
        entry.insert(0, "mybb")
        self.assertEqual(self.item.get("InternalName"), "mybb")
        self.assertTrue(self.fa.changes[-1]["label"])

    def test_links_only_web(self):
        from paintbooth.gui import tpleditor

        ed = self.editor()
        labels = {lb.cget("text"): lb for lb in self._labels(ed.sf.inner)}
        self.assertEqual(str(labels["Tutorial video"].cget("style")), "Link.TLabel")
        for bad in ("Evil share", "Script"):
            self.assertEqual(str(labels[bad].cget("style")), "Muted.TLabel")
            self.assertEqual(labels[bad].bind("<Button-1>"), "")  # not clickable
        with mock.patch.object(tpleditor.webbrowser, "open") as op:
            labels["Tutorial video"].event_generate("<Button-1>")
            self.pump()
        op.assert_called_once_with("https://www.youtube.com/watch?v=x")

    def test_image_list_and_limit(self):
        from paintbooth.gui.tpleditor import ImageList

        ed = self.editor()
        il = next(w for v, ws, _ in ed.rows for w in ws if isinstance(w, ImageList))
        self.assertEqual(il._limit(), "3")
        il._add(["a.png", "b.png"])
        il._move(0, 1)
        self.assertEqual(il.paths, ["b.png", "a.png"])
        il._remove(0)
        self.assertEqual(il.paths, ["a.png"])


@needs_display
class TutorialsTest(TkCase):
    def test_links_are_checked(self):
        from paintbooth.gui import tpleditor, tutorials

        w = tutorials.open_tutorials(self.app(), "Getting template packs")
        tags = [t for t in w.text.tag_names() if t.startswith("link") and t != "link"]
        self.assertEqual(len(tags), 2)  # the two forum links
        w._write("[bad](file:///C:/x.exe) and [good](https://example.com)")
        tags = [
            t for t in w.text.tag_names() if t.startswith("link") and t != "link" and w.text.tag_ranges(t)
        ]
        self.assertEqual(len(tags), 1)
        with mock.patch.object(tpleditor.webbrowser, "open") as op:
            # the good link is clickable (Text.tag_bind can't query, so ask Tk directly)
            self.assertTrue(w.text.tk.call(w.text._w, "tag", "bind", tags[0], "<Button-1>"))
            tutorials._open_link("https://example.com")
            tutorials._open_link("file:///C:/x.exe")
        op.assert_called_once_with("https://example.com")


# ---------------------------------------------------------------------------------- helpers
@needs_display
class DebouncerTest(TkCase):
    def test_coalesces_and_flushes(self):
        from paintbooth.gui.editors import Debouncer

        calls = []
        d = Debouncer(self.root, 10_000, lambda: calls.append(1))
        for _ in range(5):
            d()
        self.assertTrue(d.pending)
        self.assertEqual(calls, [])
        d.flush()
        self.assertEqual(calls, [1])
        self.assertFalse(d.pending)
        d.flush()  # nothing pending: no call
        self.assertEqual(calls, [1])

    def test_dropped_after_destroy(self):
        from paintbooth.gui.editors import Debouncer

        calls = []
        f = tk.Frame(self.root)
        d = Debouncer(f, 1, lambda: calls.append(1))
        d()
        f.destroy()
        d._run()  # what the timer would do
        self.assertEqual(calls, [])


@needs_display
class ColorPickTest(TkCase):
    def test_settings_are_explicit(self):
        """F16: the colour panel writes to the settings it is given, not a hidden global."""
        from paintbooth.gui import colorpick

        mine: dict = {}
        panel = colorpick.ColorPanel(self.root, (10, 20, 30), settings=mine)
        panel.save_current()
        self.assertEqual(mine["saved_colors"], ["#0a141e"])
        colorpick.remember_recent((1, 2, 3), mine)
        self.assertEqual(colorpick.recent_colors(mine), ["#010203"])

    def test_unconfigured_is_harmless(self):
        from paintbooth.gui import colorpick

        colorpick.configure(None)
        colorpick.remember_recent((1, 2, 3))  # goes to a throw-away dict
        self.assertEqual(colorpick.recent_colors(), [])
        fa = self.app()
        colorpick.configure(fa)
        colorpick.remember_recent((1, 2, 3))
        self.assertEqual(fa.settings["recent_colors"], ["#010203"])

    def test_converter_uses_util(self):
        from paintbooth.gui.tools import ColorConverter

        cc = ColorConverter(self.app())
        cc.hex.set("#808080")
        cc._from_hex()
        self.assertEqual(cc.out.get(), "(0.2159, 0.2159, 0.2159)")


@needs_display
class CodeEditorTest(TkCase):
    def test_find_replace_and_comment(self):
        from paintbooth.gui.codeeditor import CodeEditor

        ed = CodeEditor(self.root)
        ed.pack()
        ed.set('<a>\n  <b x="1"/>\n</a>')
        ed.find_var.set("b")
        ed.open_find()
        self.assertEqual(ed._matches, ["2.3"])
        ed.repl_var.set("c")
        ed.replace_all()
        self.assertEqual(ed.get(), '<a>\n  <c x="1"/>\n</a>')
        ed.text.mark_set("insert", "2.0")
        ed._toggle_comment(None)
        self.assertEqual(ed.get().split("\n")[1], '  <!-- <c x="1"/> -->')
        ed._toggle_comment(None)
        self.assertEqual(ed.get().split("\n")[1], '  <c x="1"/>')


if __name__ == "__main__":
    unittest.main()
