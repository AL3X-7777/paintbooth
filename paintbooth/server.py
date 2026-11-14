"""Local, offline browser UI for a ``project.xml`` project (``paintbooth ui``).

The server listens on 127.0.0.1 only and needs no internet. It serves ``ui/index.html``
and a small JSON API on top of a :class:`~paintbooth.session.Session`:

==========================  ======================================================
``GET  /``                  the UI page (with this session's token filled in)
``GET  /api/state``         project, problems, build and log; ``?since=<version>``
                            long-polls until something changes (25 s at most)
``GET  /api/file?path=``    text of an editable project file (.xml/.txt/.sii)
``GET  /api/thumb?path=``   a 256 px PNG preview of a project image
``POST /api/build``         start a build
``POST /api/autobuild``     ``{"on": bool}``
``POST /api/reload``        reload the project
``POST /api/file``          ``{"path": ..., "text": ...}`` save a project file
==========================  ======================================================

Security (a web page in the user's browser can reach 127.0.0.1 too):

* the ``Host`` header must be ``127.0.0.1:<port>`` or ``localhost:<port>``. This stops
  DNS rebinding, where ``evil.example`` is re-pointed at 127.0.0.1 so its scripts
  become "same origin" with this server;
* an ``Origin`` header, when sent, must be this server's own origin (``null`` is refused);
* every ``/api/`` request must carry the per-session random token, which only the page
  served by this server knows: the ``X-Paintbooth-Token`` header, or ``?token=`` for
  ``<img>`` thumbnails;
* paths are resolved inside the project folder, and only .xml/.txt/.sii files can be
  read or written; request bodies are limited to :data:`MAX_BODY` bytes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from PIL import Image

from .session import Session

log = logging.getLogger(__name__)

UI_FILE = Path(__file__).with_name("ui") / "index.html"
TOKEN_PLACEHOLDER = "__PAINTBOOTH_TOKEN__"  # noqa: S105 (a marker in index.html, not a secret)
TOKEN_HEADER = "X-Paintbooth-Token"  # noqa: S105 (a header name, not a secret)
EDITABLE = {".xml", ".txt", ".sii"}
MAX_BODY = 4 * 1024 * 1024  # a project file edited in the browser; far more than any real one
THUMB_CACHE_SIZE = 128
THUMB_BOX = (256, 256)
LONG_POLL_SECONDS = 25


def _state(session: Session) -> dict[str, Any]:
    """Return everything the UI shows, as JSON-ready data."""
    with session.lock:
        p = session.project
        b = session.last_build
        files = []
        for f in [p.file, *sorted({x for d in p.vehicles_dirs if d.is_dir() for x in d.glob("*.xml")})]:
            files.append(p.rel(f))
        if p.mod and p.mod.description:
            files.append(p.rel(p.mod.description))
        issues = list(p.issues)
        if b:
            issues += [i for i in b.issues if i not in issues]
        return {
            "version": session.version,
            "project_dir": str(session.dir),
            "mod": None
            if p.mod is None
            else {
                "package": p.mod.package,
                "title": p.mod.title,
                "author": p.mod.author,
                "version": p.mod.version,
                "game": p.mod.game,
            },
            "output": p.rel(p.output) if p.output else None,
            "skins": [
                {
                    "id": s.id,
                    "name": s.name,
                    "price": s.price,
                    "mode": s.mode,
                    "line": s.line,
                    "targets": [
                        {"vehicle": t.vehicle, "slot": t.slot, "image": p.rel(t.image), "line": t.line}
                        for t in s.targets
                    ],
                }
                for s in p.skins
            ],
            "vehicles": [
                {
                    "id": v.id,
                    "name": v.name,
                    "kind": v.kind,
                    "file": v.file,
                    "line": v.line,
                    "template": v.template,
                    "slots": [{"id": s.id, "main": s.main, "accs": s.accs} for s in v.slots.values()],
                }
                for v in sorted(p.vehicles.values(), key=lambda v: v.id)
            ],
            "issues": [
                {"level": i.level, "file": i.file, "line": i.line, "message": i.message} for i in issues
            ],
            "build": None
            if b is None
            else {
                "ok": b.ok,
                "seconds": round(b.seconds, 2),
                "files": b.files,
                "encoded": b.textures_encoded,
                "cached": b.textures_cached,
                "size": b.size_bytes,
                "output": p.rel(b.output) if b.output else None,
            },
            "building": session.building,
            "auto_build": session.auto_build,
            "log": list(session.log_lines)[-120:],
            "files": files,
        }


class RequestError(Exception):
    """A request the API refuses; answered with ``status`` and ``message`` as JSON."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ThumbCache:
    """Least-recently-used cache of PNG thumbnails, keyed by (path, mtime, size)."""

    def __init__(self, size: int = THUMB_CACHE_SIZE):
        self.size = size
        self._items: OrderedDict[tuple[Path, int, int], bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, p: Path) -> bytes | None:
        """Return a PNG preview of image ``p``, or None if it is not a readable image."""
        st = p.stat()
        key = (p, st.st_mtime_ns, st.st_size)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        try:
            with Image.open(p) as im:
                im.draft("RGB", THUMB_BOX)  # JPEG: decode at reduced size, much faster
                small = im.convert("RGBA")
                small.thumbnail(THUMB_BOX)
                buf = io.BytesIO()
                small.save(buf, "PNG")
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            log.debug("no thumbnail for %s: %s", p, e)
            return None
        data = buf.getvalue()
        with self._lock:
            self._items[key] = data
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return data

    def __len__(self) -> int:
        return len(self._items)


class Handler(BaseHTTPRequestHandler):
    """Request handler; :func:`make_handler` makes a subclass bound to one session."""

    session: ClassVar[Session]
    token: ClassVar[str]
    thumbs: ClassVar[ThumbCache]

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the console quiet (the session log shows what matters)."""

    # ------------------------------------------------------------ responses
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        """Send a complete response that the browser must not cache."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        """Send ``obj`` as JSON."""
        self._send(code, json.dumps(obj).encode())

    # ------------------------------------------------------------ checks
    def _allowed_hosts(self) -> set[str]:
        """Return the ``Host`` values this server answers to."""
        port = self.server.server_address[1]  # type: ignore[index]  # always (host, port) for TCP
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check_request(self, path: str, query: dict[str, str]) -> None:
        """Refuse requests from other sites (see the module docstring).

        Raises:
            RequestError: The Host, Origin or token is wrong.
        """
        hosts = self._allowed_hosts()
        if self.headers.get("Host", "") not in hosts:
            raise RequestError(403, "unknown Host")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            raise RequestError(403, "cross-origin request refused")
        if path.startswith("/api/"):
            sent = self.headers.get(TOKEN_HEADER) or query.get("token", "")
            if not secrets.compare_digest(sent.encode(), self.token.encode()):
                raise RequestError(403, "missing or wrong session token")

    def _body(self) -> dict[str, Any]:
        """Return the JSON object sent with a POST.

        Raises:
            RequestError: The body is too large, not JSON, or not an object.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise RequestError(400, "bad Content-Length") from None
        if n < 0 or n > MAX_BODY:
            raise RequestError(413, "request too large")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise RequestError(400, "body is not JSON") from None
        if not isinstance(data, dict):
            raise RequestError(400, "body must be a JSON object")
        return data

    def _safe_path(self, rel: object) -> Path | None:
        """Return project-relative path ``rel`` resolved inside the project folder, or None."""
        if not isinstance(rel, str) or not rel or "\x00" in rel:
            return None
        root = self.session.dir
        p = (root / rel).resolve()
        return p if p.is_relative_to(root) else None

    # ------------------------------------------------------------ dispatch
    def _dispatch(self, method: str) -> None:
        """Check and route a request; turn :class:`RequestError` into a JSON error."""
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = self._GET if method == "GET" else self._POST
        try:
            self._check_request(url.path, query)
            route = routes.get(url.path)
            if route is None:
                self._send(404, b"not found", "text/plain")
                return
            route(self, query)
        except RequestError as e:
            self._json({"error": e.message}, e.status)

    def do_GET(self) -> None:
        """Handle a GET request."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Handle a POST request."""
        self._dispatch("POST")

    # ------------------------------------------------------------ GET routes
    def _get_page(self, _q: dict[str, str]) -> None:
        """Serve the UI with the session token filled in."""
        html = UI_FILE.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, self.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _get_state(self, q: dict[str, str]) -> None:
        """Serve the state; with ``since``, wait for a change first."""
        try:
            since = int(q.get("since", -1))
        except ValueError:
            raise RequestError(400, "since must be a whole number") from None
        if since >= 0:
            self.session.wait_for_change(since, LONG_POLL_SECONDS)
        self._json(_state(self.session))

    def _get_file(self, q: dict[str, str]) -> None:
        """Serve the text of an editable project file."""
        p = self._safe_path(q.get("path", ""))
        if p is None or p.suffix.lower() not in EDITABLE or not p.is_file():
            raise RequestError(404, "not an editable project file")
        try:
            text = p.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            raise RequestError(500, f"cannot read {q['path']}: {e}") from None
        self._json({"path": q["path"], "text": text})

    def _get_thumb(self, q: dict[str, str]) -> None:
        """Serve a PNG thumbnail of a project image."""
        p = self._safe_path(q.get("path", ""))
        if p is None or not p.is_file():
            self._send(404, b"", "text/plain")
            return
        data = self.thumbs.get(p)
        if data is None:
            self._send(415, b"", "text/plain")
            return
        self._send(200, data, "image/png")

    # ------------------------------------------------------------ POST routes
    def _post_build(self, _q: dict[str, str]) -> None:
        """Start a build in the background."""
        threading.Thread(target=self.session.build, daemon=True).start()
        self._json({"started": True})

    def _post_autobuild(self, _q: dict[str, str]) -> None:
        """Turn auto-build on or off."""
        self.session.set_auto_build(bool(self._body().get("on")))
        self._json({"auto_build": self.session.auto_build})

    def _post_reload(self, _q: dict[str, str]) -> None:
        """Reload the project."""
        self.session.reload("manual")
        self._json({"ok": True})

    def _post_file(self, _q: dict[str, str]) -> None:
        """Save a project file atomically, then reload."""
        body = self._body()
        p = self._safe_path(body.get("path", ""))
        text = body.get("text", "")
        if p is None or p.suffix.lower() not in EDITABLE:
            raise RequestError(400, "can only save .xml/.txt/.sii files inside the project")
        if not isinstance(text, str):
            raise RequestError(400, "text must be a string")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="")
            os.replace(tmp, p)
        except OSError as e:
            raise RequestError(500, f"cannot save {body['path']}: {e}") from None
        self.session.reload(f"saved {body['path']}")
        self._json({"ok": True})

    _GET: ClassVar[dict[str, Any]] = {
        "/": _get_page,
        "/api/state": _get_state,
        "/api/file": _get_file,
        "/api/thumb": _get_thumb,
    }
    _POST: ClassVar[dict[str, Any]] = {
        "/api/build": _post_build,
        "/api/autobuild": _post_autobuild,
        "/api/reload": _post_reload,
        "/api/file": _post_file,
    }


def make_handler(session: Session, token: str | None = None) -> type[Handler]:
    """Return a request handler class bound to ``session``.

    Args:
        session: The project session the API works on.
        token: The secret every API request must carry; a new random one when None.
            It is available as the returned class's ``token`` attribute.
    """
    return type(
        "SessionHandler",
        (Handler,),
        {"session": session, "token": token or secrets.token_urlsafe(24), "thumbs": ThumbCache()},
    )


def serve(project_dir: Path, port: int = 8765, open_browser: bool = True, auto_build: bool = False) -> None:
    """Run the browser UI for ``project_dir`` until Ctrl+C.

    Args:
        project_dir: The ``project.xml`` project folder.
        port: The first port to try; the next 19 are tried if it is taken.
        open_browser: Open the UI in the default browser.
        auto_build: Rebuild after every change.

    Raises:
        OSError: No port in the range is free.
    """
    session = Session(project_dir, auto_build=auto_build, log_to_console=True)
    session.start()
    if auto_build and not session.project.errors:
        threading.Thread(target=session.build, daemon=True).start()
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(session))
            break
        except OSError:
            continue
    if server is None:
        session.stop()
        raise OSError(f"No free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Paintbooth UI running at {url}  (Ctrl+C to quit)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # Ctrl+C is the normal way to stop
    finally:
        session.stop()
        server.server_close()
