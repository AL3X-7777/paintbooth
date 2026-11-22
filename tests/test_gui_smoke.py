"""Headless smoke and regression tests for the Paintbooth main window (group C1).

The window tests need an X display (on CI: ``xvfb-run python -m unittest``) and a Python built with
Tk; without either they are skipped. The settings-file and auto-version tests need no display.
"""

import _isolation  # noqa: I001  (must come first: isolates the user folders)

import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
except ImportError:  # Python built without Tk
    tk = None  # type: ignore[assignment]


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


class SettingsFileTest(unittest.TestCase):
    """F21: atomic writes, a backup, and no silent reset on a damaged file."""

    def setUp(self):
        from paintbooth.gui import prefs  # needs no Tk

        self.prefs = prefs
        self.dir = Path(tempfile.mkdtemp(dir=_isolation.TEMP_HOME))
        self.path = self.dir / ".paintbooth.json"

    def test_round_trip_and_backup(self):
        self.assertTrue(self.prefs.save_settings({"a": 1}, self.path))
        self.assertTrue(self.prefs.save_settings({"a": 2}, self.path))
        self.assertEqual(self.prefs.load_settings(self.path), {"a": 2})
        self.assertEqual(json.loads((self.dir / ".paintbooth.json.bak").read_text()), {"a": 1})
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()), [".paintbooth.json", ".paintbooth.json.bak"]
        )

    def test_damaged_file_is_kept_and_backup_used(self):
        self.prefs.save_settings({"recent": ["x"]}, self.path)
        self.prefs.save_settings({"recent": ["y"]}, self.path)
        self.path.write_text('{"recent": [', encoding="utf-8")  # cut off mid-write
        data, problem = self.prefs.read_settings(self.path)
        self.assertEqual(data, {"recent": ["x"]})
        self.assertIn("backup", problem)
        self.assertEqual((self.dir / ".paintbooth.json.broken").read_text(), '{"recent": [')

    def test_damaged_file_without_backup(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.assertLogs("paintbooth.gui.prefs", "ERROR"):
            data, problem = self.prefs.read_settings(self.path)
        self.assertEqual(data, {})
        self.assertIn("default settings", problem)
        self.assertTrue((self.dir / ".paintbooth.json.broken").exists())

    def test_failed_write_leaves_old_file(self):
        self.prefs.save_settings({"a": 1}, self.path)
        self.assertFalse(self.prefs.save_settings({"bad": object()}, self.path))
        self.assertEqual(self.prefs.load_settings(self.path), {"a": 1})
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_missing_file(self):
        self.assertEqual(self.prefs.read_settings(self.dir / "none.json"), ({}, None))


class ImportSideEffectsTest(unittest.TestCase):
    """F16: importing the GUI must not move folders or touch the settings file."""

    def test_import_has_no_side_effects(self):
        if tk is None:
            self.skipTest("tkinter not available")
        import importlib

        from paintbooth import appdirs

        with mock.patch.object(appdirs, "migrate") as mig, mock.patch.object(appdirs, "settings_file") as sf:
            import paintbooth.gui.main as m

            importlib.reload(m)
        mig.assert_not_called()
        sf.assert_not_called()


@unittest.skipUnless(HAVE_DISPLAY, "needs Tk and a display (run under xvfb-run)")
class MainWindowTest(unittest.TestCase):
    """Builds the real main window, opens a project, switches pages, opens Settings and closes."""

    def setUp(self):
        from paintbooth.gui import icons, prefs
        from paintbooth.gui import main as gm
        from paintbooth.ms2 import Ms2Project

        self.gm = gm
        from paintbooth import tplbuild

        # App() hands its settings to tplbuild; undo that so later test modules start clean.
        self.addCleanup(tplbuild.configure, None)
        icons._shared.clear()  # the shared icon cache belongs to the previous test's Tk root
        prefs.settings_path().parent.mkdir(parents=True, exist_ok=True)
        prefs.save_settings({"check_updates": False})
        self.dir = Path(tempfile.mkdtemp(dir=_isolation.TEMP_HOME))
        self.proj_path = self.dir / "Sample.pb"
        Ms2Project.new("ets2").save(self.proj_path)
        # A modal message box would hang a headless run: record them instead.
        self.dialogs = []
        from tkinter import messagebox

        for name, answer in (
            ("showerror", None),
            ("showinfo", None),
            ("showwarning", None),
            ("askyesno", False),
            ("askokcancel", False),
            ("askyesnocancel", False),
        ):
            patcher = mock.patch.object(
                messagebox,
                name,
                side_effect=lambda *a, _n=name, _r=answer, **k: self.dialogs.append((_n, a)) or _r,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = tk.Tk()
        self.addCleanup(self._destroy_root)  # also runs when App() fails, so the next test gets a fresh root
        self.errors = []
        self.root.report_callback_exception = lambda *a: self.errors.append(a)
        self.app = gm.App(self.root, None)

    def _dialog(self, name, answer):
        def fake(*args, **_kw):
            self.dialogs.append((name, args))
            return answer

        return fake

    def _destroy_root(self):
        app = getattr(self, "app", None)
        if app is not None:
            app.exports.close()
        with contextlib.suppress(tk.TclError):  # already destroyed by App.quit()
            self.root.destroy()

    def pump(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def test_ribbon_customising_add_popup_drops_and_reset(self):
        from unittest import mock

        from paintbooth.gui import ribbon as rb
        from paintbooth.gui.settings import SettingsWindow

        app = self.app
        self.pump()
        r = app.ribbon
        self.assertEqual([g["caption"] for g in r.layout][:2], ["Project", "Items"])
        # move "Delete" to the front of Project, then add a lightbox button to Export
        r.drop("delete", 0, 0)
        self.assertEqual(r.layout[0]["items"][0], "delete")
        self.assertIn("delete", r.buttons)
        r.drop("add:TruckLightbox", 2, None)
        self.assertEqual(r.layout[2]["items"][-1], "add:TruckLightbox")
        self.assertEqual(app.settings["ribbon_layout"], r.layout)  # remembered
        # a drop target is found from the screen point of a button
        b = r.buttons["export"]
        self.pump()
        target = r.drop_target(b.winfo_rootx() + 2, b.winfo_rooty() + 2)
        self.assertEqual(target, (2, 0))
        # the Add more list opens (hover or click) and closes
        app.show_add_menu()
        self.pump()
        self.assertIsNotNone(r.add_popup)
        r.close_add_popup()
        self.assertIsNone(r.add_popup)
        # Settings > Reset to defaults puts the ribbon (and the settings) back
        app.settings["creator_name"] = "Someone"
        sw = SettingsWindow(app)
        with mock.patch("paintbooth.gui.settings.messagebox.askyesno", return_value=True):
            sw.reset_defaults()
        self.assertNotIn("creator_name", app.settings)
        self.assertEqual(r.layout, rb.DEFAULT_LAYOUT)
        self.assertFalse(self.errors, self.errors)

    def test_open_switch_settings_close(self):
        app = self.app
        self.pump()
        self.assertEqual(list(app.ribbon.tab_btns), ["Home"])
        app.open_project(self.proj_path)
        self.pump()
        self.assertIs(app.shown, app.manifest_editor)
        self.assertEqual(app.project.game, "ets2")
        app.add_item()  # switches to the truck skin editor
        self.pump()
        self.assertIs(app.shown, app.truck_editor)
        app.items_tv.selection_set("manifest")
        self.pump()
        self.assertIs(app.shown, app.manifest_editor)

        app._show_update("9.9.9", "https://example.invalid/")
        self.assertIn("9.9.9", app.update_lbl.cget("text"))
        self.assertTrue(app.update_lbl.winfo_manager())
        app.notice.show("Sample.scs exported successfully to", "", ("/tmp", lambda: None))
        self.assertIsNotNone(app.notice.frame)
        app.notice.shown_at = 0
        app.notice._any(None)
        self.assertIsNone(app.notice.frame)

        from paintbooth.gui.settings import SettingsWindow

        sw = SettingsWindow(app)
        self.pump()
        self.assertTrue(sw.win.winfo_exists())
        self.assertEqual(sw.vars["ms2_dir"].get(), "")
        sw.win.destroy()  # saves the window position
        self.assertIn("settings", app.settings["windows"])
        self.assertFalse(self.errors, self.errors)

        self.assertFalse(self.dialogs, self.dialogs)
        app.project.manifest.vars.set_value("description", "x")  # unsaved change: quit must ask
        app.quit()  # the stub answers "No" (don't save)
        self.assertEqual([d[0] for d in self.dialogs], ["askyesnocancel"])
        from paintbooth.gui import prefs

        self.assertIn("geometry", prefs.load_settings())  # quit saved the window layout

    def test_export_reads_a_snapshot(self):
        """F8: the worker thread gets a copy of the project, not the tree the UI edits."""
        app = self.app
        app.open_project(self.proj_path)
        self.pump()
        seen = {}
        started = threading.Event()

        def fake_plan(project, overrides, icons):
            seen["project"] = project
            started.set()
            return [], []

        from paintbooth.builder import BuildResult

        with (
            mock.patch("paintbooth.gui.main.validate", return_value=[]),  # a new project has no skin yet
            mock.patch("paintbooth.gui.export_ctl.plan_ms2", fake_plan),
            mock.patch.object(app.exports.builder, "build_files", return_value=BuildResult(ok=True)),
        ):
            app.settings["export_notice"] = False
            app.export()
            self.assertTrue(started.wait(10))
            self.pump(0.3)
        self.assertIsNot(seen["project"], app.project)
        self.assertIsNot(seen["project"].root, app.project.root)
        self.assertEqual(seen["project"].path, app.project.path)
        self.assertFalse(app.exports.exporting)

    def test_auto_version_saves_only_the_version(self):
        """F31: automatic versioning must not save unrelated unsaved edits."""
        from paintbooth.ms2 import Ms2Project

        app = self.app
        app.open_project(self.proj_path)
        self.pump()
        project = app.project
        project.manifest.vars.set_value("description", "unsaved edit")
        project.manifest.vars.set_value("package_version", "1.05")
        app.projects.save_version(project, "1.05")
        on_disk = Ms2Project.load(self.proj_path).manifest.vars
        self.assertEqual(on_disk.value("package_version"), "1.05")
        self.assertNotEqual(on_disk.value("description"), "unsaved edit")
        self.assertTrue(project.dirty)  # the edit is still unsaved, and can still be discarded
        self.assertEqual(app.projects.mtime, self.proj_path.stat().st_mtime_ns)  # no "changed outside"

        # when the version is the only change, it is an ordinary save
        project.manifest.vars.set_value("description", on_disk.value("description"))
        project.manifest.vars.set_value("package_version", "1.06")
        app.projects.save_version(project, "1.06")
        self.assertFalse(project.dirty)
        self.assertEqual(Ms2Project.load(self.proj_path).manifest.vars.value("package_version"), "1.06")

    def test_thumbcache_dedups_and_is_bounded(self):
        """F35/F18: one decode per image however many requests, and a capped cache."""
        from PIL import Image

        img = self.dir / "a.png"
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(img)
        tc = self.app.thumbs
        got = []
        with mock.patch("paintbooth.gui.widgets.threading.Thread") as th:
            th.return_value.start = lambda: None
            tc.request(img, (32, 32), got.append)
            tc.request(img, (32, 32), got.append)
            self.assertEqual(th.call_count, 1)
        key = next(iter(tc.pending))
        tc._finish(key, "IMG")
        self.assertEqual(got, ["IMG", "IMG"])
        for i in range(tc.MAX_ITEMS + 10):
            tc._finish((str(i), 0, (1, 1)), None)
        self.assertEqual(len(tc.cache), tc.MAX_ITEMS)


if __name__ == "__main__":
    unittest.main()
