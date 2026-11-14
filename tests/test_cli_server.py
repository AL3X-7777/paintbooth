"""Command line (``__main__``), live session and the local browser server.

Regression tests for review findings F3 (Host/Origin/token checks), F23 (input
validation and body limit), F35 (bounded thumbnail cache), F16 (settings passed to
tplbuild) and F34 (HashFS input reported as an error, not a crash).
"""

import _isolation  # noqa: F401, I001  (must come first: isolates the user folders)

import contextlib
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

from paintbooth import __main__ as cli
from paintbooth import appdirs, server, session
from paintbooth.ms2 import Ms2Project
from paintbooth.server import MAX_BODY, ThumbCache, make_handler
from paintbooth.session import Session

VEHICLE = """<vehicle id="scania.s_2016" kind="truck" game="ets2">
  <slot id="default" main="true"/>
</vehicle>"""
SKIN = """<skin id="blue" name="Blue" price="10" base_color="#ffffff">
    <target vehicle="scania.s_2016" image="art/a.png"/>
  </skin>"""


def make_project(root: Path) -> Path:
    """A buildable project.xml project with one skin."""
    (root / "vehicles").mkdir(parents=True)
    (root / "art").mkdir()
    Image.new("RGBA", (64, 64), (10, 20, 200, 255)).save(root / "art/a.png")
    (root / "art/not_an_image.txt").write_text("hello")
    (root / "vehicles/v.xml").write_text(VEHICLE)
    (root / "project.xml").write_text(
        f'<project>\n  <mod package="testpack" title="T" author="me" game="ets2"/>\n  {SKIN}\n</project>'
    )
    return root


def quiet():
    """Swallow what the CLI prints."""
    return contextlib.redirect_stdout(io.StringIO())


# ------------------------------------------------------------------ server
class ServerApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.tmp.name) / "p")
        with quiet():
            self.session = Session(self.root)
        self.handler = make_handler(self.session, token="secret-token")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.session.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, token=True, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-Paintbooth-Token": "secret-token"} if token else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, path, data, h)
        r = c.getresponse()
        out = r.status, r.read(), r.getheader("Content-Type")
        c.close()
        return out

    def test_page_carries_token(self):
        status, body, ctype = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b'content="secret-token"', body)
        self.assertNotIn(server.TOKEN_PLACEHOLDER.encode(), body)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertEqual(len(make_handler(self.session).token), 32)  # random when not given

    def test_rebinding_and_cross_origin_are_refused(self):  # F3
        body = {"path": "project.xml", "text": "<project/>"}
        evil = f"evil.example:{self.port}"
        for headers in (
            {"Host": evil, "Origin": f"http://{evil}"},
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
        ):
            status, _, _ = self.request("POST", "/api/file", body, headers)
            self.assertEqual(status, 403, headers)
        status, _, _ = self.request("POST", "/api/file", body, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/state", headers={"X-Paintbooth-Token": "wrong"}, token=False)
        self.assertEqual(status, 403)
        self.assertIn("testpack", (self.root / "project.xml").read_text())
        ok = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"}
        self.assertEqual(self.request("GET", "/api/state", headers=ok)[0], 200)
        self.assertEqual(self.request("GET", "/api/state?token=secret-token", token=False)[0], 200)

    def test_state_and_bad_input(self):  # F23
        status, body, _ = self.request("GET", "/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["mod"]["package"], "testpack")
        self.assertEqual(state["skins"][0]["id"], "blue")
        self.assertIn("project.xml", state["files"])
        status, body, _ = self.request("GET", f"/api/state?since={state['version'] - 1}")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/state?since=abc")[0], 400)
        self.assertEqual(self.request("POST", "/api/file", raw="{not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/file", raw="[1, 2]")[0], 400)
        self.assertEqual(self.request("POST", "/api/file", {"path": "x.xml", "text": 5})[0], 400)
        self.assertEqual(self.request("POST", "/api/file", {"path": 5})[0], 400)
        status, _, _ = self.request(
            "POST", "/api/file", raw="", headers={"Content-Length": str(MAX_BODY + 1)}
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.request("POST", "/api/file", raw="", headers={"Content-Length": "x"})[0], 400)
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {})[0], 404)

    def test_files(self):
        status, body, _ = self.request("GET", "/api/file?path=project.xml")
        self.assertEqual(status, 200)
        self.assertIn("testpack", json.loads(body)["text"])
        for bad in ("../outside.xml", "art/a.png", "missing.xml", ""):
            self.assertEqual(self.request("GET", f"/api/file?path={bad}")[0], 404, bad)
        new = (self.root / "project.xml").read_text().replace('title="T"', 'title="New"')
        status, _, _ = self.request("POST", "/api/file", {"path": "project.xml", "text": new})
        self.assertEqual(status, 200)
        self.assertEqual(self.session.project.mod.title, "New")
        status, _, _ = self.request("POST", "/api/file", {"path": "../escape.xml", "text": "x"})
        self.assertEqual(status, 400)
        self.assertFalse((self.root.parent / "escape.xml").exists())

    def test_thumbnails(self):
        status, body, ctype = self.request("GET", "/api/thumb?path=art/a.png")
        self.assertEqual((status, ctype), (200, "image/png"))
        self.assertEqual(Image.open(io.BytesIO(body)).size, (64, 64))
        self.assertEqual(self.request("GET", "/api/thumb?path=art/a.png")[1], body)  # cached
        self.assertEqual(self.request("GET", "/api/thumb?path=art/not_an_image.txt")[0], 415)
        self.assertEqual(self.request("GET", "/api/thumb?path=../x.png")[0], 404)

    def test_actions(self):
        self.assertEqual(self.request("POST", "/api/reload", {})[0], 200)
        _, body, _ = self.request("POST", "/api/autobuild", {"on": False})
        self.assertEqual(json.loads(body), {"auto_build": False})
        with mock.patch.object(self.session, "build") as build:
            self.assertEqual(self.request("POST", "/api/build", {})[0], 200)
            time.sleep(0.2)
        build.assert_called_once()


class Thumbs(unittest.TestCase):
    def test_cache_is_bounded(self):  # F35
        with tempfile.TemporaryDirectory() as t:
            cache = ThumbCache(size=3)
            for i in range(6):
                f = Path(t) / f"{i}.png"
                Image.new("RGB", (8, 8)).save(f)
                self.assertIsNotNone(cache.get(f))
            self.assertEqual(len(cache), 3)


class Serve(unittest.TestCase):
    def test_serve_until_ctrl_c(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(Path(t) / "p")
            with (
                quiet() as out,
                mock.patch.object(ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt),
            ):
                server.serve(root, port=0, open_browser=False)
            self.assertIn("http://127.0.0.1:", out.getvalue())

    def test_no_free_port(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(Path(t) / "p")
            with (
                quiet(),
                mock.patch.object(server, "ThreadingHTTPServer", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                server.serve(root, port=9000, open_browser=False)


# ------------------------------------------------------------------ session
class LiveSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.tmp.name) / "p")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reload_on_change_and_build(self):
        with quiet():
            s = Session(self.root, log_to_console=True)
        try:
            v = s.version
            self.assertEqual(s.wait_for_change(v - 1, 0.01), v)
            text = (self.root / "project.xml").read_text().replace('title="T"', 'title="Changed"')
            time.sleep(0.01)
            (self.root / "project.xml").write_text(text + "\n")
            with quiet(), mock.patch.object(session, "DEBOUNCE_SECONDS", 0):
                s._check_once()
                s._check_once()  # nothing changed since: no reload
            self.assertEqual(s.project.mod.title, "Changed")
            self.assertTrue(any("changed: project.xml" in x for x in s.log_lines))
            with quiet():
                r = s.build()
            self.assertTrue(r.ok)
            self.assertTrue((self.root / "build/testpack.scs").is_file())
            s.building = True
            self.assertIs(s.build(), r)  # already building: previous result
            s.building = False
            with quiet(), mock.patch.object(s.builder, "build", side_effect=RuntimeError("boom")):
                r2 = s.build()
            self.assertFalse(r2.ok)
            self.assertTrue(any("Build failed" in x for x in s.log_lines))
            with quiet(), mock.patch.object(s, "build") as build:
                s.set_auto_build(True)
                time.sleep(0.1)
                s.reload("x")
            self.assertGreaterEqual(build.call_count, 2)
        finally:
            s.stop()

    def test_watcher_thread_survives_errors(self):
        s = Session(self.root)
        with (
            mock.patch.object(session, "POLL_SECONDS", 0.01),
            mock.patch.object(s, "_check_once", side_effect=[RuntimeError("x")] + [None] * 1000),
        ):
            s.start()
            time.sleep(0.2)
        s.stop()
        self.assertTrue(any("Watcher error" in x for x in s.log_lines))


# ------------------------------------------------------------------ command line
class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        from paintbooth import tplbuild

        # "export" hands the settings to tplbuild; do not leak them into other tests
        if hasattr(tplbuild, "configure"):
            tplbuild.configure(None)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        """Run ``paintbooth argv``; return (exit code, printed text)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                cli.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_new_check_build(self):
        d = self.dir / "Mod"
        code, out = self.run_cli("new", str(d), "--title", "Tom & Jerry", "--game", "ats")
        self.assertEqual(code, 0)
        self.assertIn("Created project", out)
        code, _ = self.run_cli("new", str(d))
        self.assertIn("already exists", code)
        p = make_project(self.dir / "p")
        code, out = self.run_cli("check", str(p))
        self.assertEqual(code, 0)
        self.assertIn("OK: 1 skin(s)", out)
        code, out = self.run_cli("build", str(p), "-j", "1", "-o", str(self.dir / "out.scs"))
        self.assertEqual(code, 0)
        with zipfile.ZipFile(self.dir / "out.scs") as z:
            self.assertNotIn("compatible_versions", z.read("manifest.sii").decode())
        (p / "project.xml").write_text("<project><mod/></project>")
        self.assertEqual(self.run_cli("check", str(p))[0], 1)

    def test_check_plan_errors(self):
        p = make_project(self.dir / "p")
        (p / "art/a.png").write_bytes(b"not a png")  # loads fine; only plan() opens the image
        code, out = self.run_cli("check", str(p))
        self.assertEqual(code, 1)
        self.assertIn("cannot open image", out)

    def test_export(self):
        Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(self.dir / "cab.png")
        p = Ms2Project.new("ets2")
        idx = p.add_like("AdvancedTruckSkin", None)
        it = p.items[idx]
        it.vehicle_var.set_text("InternalName", "acme.hauler")
        it.set("InternalName", "abc")
        it.set("SkinName", "Test")
        e = it.add_entry("CabVariantSkin")
        e.image = str(self.dir / "cab.png")
        e.suitable = ["day.acme.hauler.cabin"]
        p.manifest.vars.set_value("display_name", "Test mod")
        f = p.save(self.dir / "mod.pb")
        appdirs.settings_file().parent.mkdir(parents=True, exist_ok=True)
        appdirs.settings_file().write_text(json.dumps({"ms2_dir": ""}))
        code, _ = self.run_cli("export", str(f))
        self.assertEqual(code, 0)
        with zipfile.ZipFile(self.dir / "mod.scs") as z:
            man = z.read("manifest.sii").decode()
        self.assertIn('"Test mod"', man)
        self.assertNotIn("compatible_versions", man)  # the user's rule (F33)
        it.set("SkinName", "")
        p.save()
        code, out = self.run_cli("export", str(f), "-o", str(self.dir / "x.scs"))
        self.assertEqual(code, 1)
        self.assertIn("ERROR: item 1: Skin name is empty", out)
        (self.dir / "broken.pb").write_text("<Project")
        code, _ = self.run_cli("export", str(self.dir / "broken.pb"))
        self.assertIn("broken.pb", code)

    def test_settings_go_to_tplbuild(self):  # F16
        from paintbooth import tplbuild

        settings_file = appdirs.settings_file()
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"ms2_dir": "C:/MS2"}))
        self.assertEqual(cli.load_settings(), {"ms2_dir": "C:/MS2"})
        with mock.patch.object(tplbuild, "configure", create=True) as conf:
            cli._configure_templates({"a": 1})
        conf.assert_called_once_with({"a": 1})
        settings_file.write_text("{broken")
        self.assertEqual(cli.load_settings(), {})
        settings_file.write_text("[1]")
        self.assertEqual(cli.load_settings(), {})
        settings_file.unlink()
        self.assertEqual(cli.load_settings(), {})

    def test_scan(self):
        base = self.dir / "src/def/vehicle/truck/acme.hauler/cabin"
        base.mkdir(parents=True)
        (base / "day.sii").write_text("SiiNunit {\naccessory_cabin_data : day.acme.hauler.cabin {\n}\n}")
        code, out = self.run_cli("scan", str(self.dir / "src"), "-o", str(self.dir / "veh"))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 1 vehicle file(s)", out)
        (self.dir / "def.scs").write_bytes(b"SCS#" + b"\0" * 32)
        code, _ = self.run_cli("scan", str(self.dir / "def.scs"))  # F34: a message, not a traceback
        self.assertIn("HashFS archive", code)

    def test_watch_ui_and_app(self):
        p = make_project(self.dir / "p")
        with (
            mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt),
            mock.patch("paintbooth.session.Session.build") as build,
        ):
            code, out = self.run_cli("watch", str(p))
        self.assertEqual(code, 0)
        self.assertIn("Watching for changes", out)
        build.assert_called()
        with mock.patch("paintbooth.server.serve", side_effect=OSError("No free port")):
            self.assertEqual(self.run_cli("ui", str(p), "--no-browser")[0], "No free port")
        with mock.patch("paintbooth.server.serve") as serve:
            self.run_cli("ui", str(p), "--port", "1234", "--auto-build")
        self.assertEqual(serve.call_args.kwargs, {"port": 1234, "open_browser": True, "auto_build": True})
        app = mock.Mock()
        # the real paintbooth.app imports the Tk GUI; a stand-in module keeps this test headless
        with mock.patch.dict(sys.modules, {"paintbooth.app": types.SimpleNamespace(main=app)}):
            self.run_cli()
            self.run_cli("app", "x.pb")
        self.assertEqual([c.args for c in app.call_args_list], [([],), (["x.pb"],)])


if __name__ == "__main__":
    unittest.main()
