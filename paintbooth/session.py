"""A live ``project.xml`` session: watches files, reloads the project, optionally rebuilds.

Used by both ``paintbooth watch`` (console) and ``paintbooth ui`` (browser, see
:mod:`paintbooth.server`). Polling is used instead of OS file events so it behaves the
same on Windows, Linux and network drives, and needs no extra packages.

Threading: the watcher thread, build threads and HTTP handler threads all share one
:class:`Session`. ``lock`` guards ``project``, ``last_build``, ``building`` and the log;
``changed`` (a condition on the same lock) wakes long-polling clients whenever
``version`` is bumped.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path

from .builder import Builder, BuildResult
from .model import Project, load_project

log = logging.getLogger(__name__)

POLL_SECONDS = 0.4
DEBOUNCE_SECONDS = 0.35


# what a watched file looked like: (mtime in ns, size), or None if it did not exist
Snapshot = dict[Path, tuple[int, int] | None]


class Session:
    """One open ``project.xml`` project with its builder, log and change counter.

    Args:
        project_dir: The project folder.
        auto_build: Rebuild after every successful reload.
        log_to_console: Also print log lines and problems (for ``paintbooth watch``).

    Attributes:
        version: Increases on every change clients should see (log line, reload, build).
        log_lines: The last 300 log lines, time-stamped.
    """

    def __init__(self, project_dir: Path, auto_build: bool = False, log_to_console: bool = False):
        self.dir = Path(project_dir).resolve()
        self.builder = Builder(self.dir / ".paintbooth" / "cache")
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.auto_build = auto_build
        self.console = log_to_console
        self.log_lines: collections.deque[str] = collections.deque(maxlen=300)
        self.project: Project = load_project(self.dir)
        self.last_build: BuildResult | None = None
        self.building = False
        self._snapshot = self._take_snapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log(f"Loaded {self.project.rel(self.project.file)}: {self._summary()}")

    # ------------------------------------------------------------------ utils
    def log(self, msg: str) -> None:
        """Add a time-stamped line to the log (and print it in console mode)."""
        line = time.strftime("%H:%M:%S ") + msg
        with self.lock:
            self.log_lines.append(line)
            self._bump()
        if self.console:
            print(line, flush=True)

    def _bump(self) -> None:
        """Increase ``version`` and wake every client waiting in :meth:`wait_for_change`."""
        with self.lock:
            self.version += 1
            self.changed.notify_all()

    def _summary(self) -> str:
        """Return a one-line count of skins, vehicles, errors and warnings."""
        p = self.project
        n_err = len(p.errors)
        n_warn = len([i for i in p.issues if i.level == "warning"])
        return f"{len(p.skins)} skin(s), {len(p.vehicles)} vehicle(s), {n_err} error(s), {n_warn} warning(s)"

    def _watch_paths(self) -> set[Path]:
        """Return the files and folders whose changes trigger a reload."""
        p = self.project
        paths = set(p.watched) | {p.file}
        for d in p.vehicles_dirs or [self.dir / "vehicles"]:
            paths.add(d)
            if d.is_dir():
                paths.update(d.glob("*.xml"))
        return paths

    def _take_snapshot(self) -> Snapshot:
        """Return the current (mtime, size) of every watched path."""
        snap: Snapshot = {}
        for path in self._watch_paths():
            try:
                st = path.stat()
                snap[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                snap[path] = None
        return snap

    # --------------------------------------------------------------- actions
    def reload(self, reason: str = "") -> None:
        """Load the project again; rebuild if auto-build is on and there are no errors."""
        with self.lock:
            self.project = load_project(self.dir)
            self._snapshot = self._take_snapshot()
        self.log(f"Reloaded{' (' + reason + ')' if reason else ''}: {self._summary()}")
        if self.console:
            for issue in self.project.issues:
                print("   ", issue, flush=True)
        if self.auto_build and not self.project.errors:
            self.build()

    def build(self) -> BuildResult | None:
        """Build the mod and return the result.

        When a build is already running, nothing is started and the previous result
        (None before the first build) is returned.
        """
        with self.lock:
            if self.building:
                return self.last_build
            self.building = True
            project = self.project
        self._bump()
        try:
            result = self.builder.build(project, log=self.log)
        except Exception as e:  # keep the session alive on unexpected failures; logged below
            log.exception("build failed")
            self.log(f"Build failed: {e!r}")
            result = BuildResult(ok=False, issues=list(project.issues))
        with self.lock:
            self.last_build = result
            self.building = False
        if self.console:
            for issue in result.issues:
                if issue not in project.issues:
                    print("   ", issue, flush=True)
        self._bump()
        return result

    def set_auto_build(self, on: bool) -> None:
        """Turn auto-build on or off; turning it on starts a build in the background."""
        self.auto_build = on
        self.log(f"Auto-build {'on' if on else 'off'}")
        if on and not self.project.errors:
            threading.Thread(target=self.build, daemon=True).start()

    # ---------------------------------------------------------------- watcher
    def _check_once(self) -> None:
        """Reload once if a watched file changed, after the files stop changing."""
        new = self._take_snapshot()
        if new == self._snapshot:
            return
        # wait until files stop changing (editors save in several steps)
        for _ in range(20):
            time.sleep(DEBOUNCE_SECONDS)
            newer = self._take_snapshot()
            if newer == new:
                break
            new = newer
        old = self._snapshot
        changed = [p for p in set(new) | set(old) if new.get(p) != old.get(p)]
        names = ", ".join(sorted({self.project.rel(p) for p in changed})[:4])
        self.reload(f"changed: {names}")

    def _loop(self) -> None:
        """Watcher thread: poll every :data:`POLL_SECONDS` until :meth:`stop`."""
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._check_once()
            except Exception as e:  # the watcher must keep running; the error is logged
                log.exception("watcher error")
                self.log(f"Watcher error: {e!r}")

    def start(self) -> None:
        """Start the watcher thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher thread and the builder's worker processes."""
        self._stop.set()
        self.builder.close()

    def wait_for_change(self, since: int, timeout: float) -> int:
        """Block until ``version`` differs from ``since`` (or ``timeout`` seconds pass); return it."""
        with self.lock:
            if self.version == since:
                self.changed.wait(timeout)
            return self.version
