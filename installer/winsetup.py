r"""Windows post-install and uninstall steps for Paintbooth, run with the bundled ``pythonw.exe``.

``Setup.exe`` (``installer/setup.c``) unpacks the program files and then runs::

    pythonw winsetup.py install [--desktop]

which, all per user (``HKEY_CURRENT_USER``, no admin rights):

1. creates ``Documents\Paintbooth`` (``paintbooth.appdirs.ensure``);
2. removes an older *Skinyard* install (the program's earlier name): its shortcuts, registry
   entries and program files, both next to the new folder and where its uninstall entry said;
3. if the previous Paintbooth install lived in another folder, removes the program files there;
4. creates the Start menu shortcut, plus a desktop icon when asked for or when one already exists;
5. writes the "Apps and features" uninstall entry, the ``.pb`` file type and "Open with" for
   ``.ms2`` (without taking ``.ms2`` away from Mods Studio 2).

The uninstall entry runs ``pythonw winsetup.py uninstall`` (``/S`` for no question), which removes
the shortcuts and registry entries and then deletes the program files from a detached ``cmd``
loop once this process has exited. ``Documents\Paintbooth`` is never touched.

Problems in steps 1-3 never stop the install; they are logged to ``%TEMP%\PaintBoothSetup.log``.
The pure helpers (path comparison, registry values, the shortcut decision, which old folders to
remove, the uninstall command) take their inputs as arguments so they can be tested on any OS.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

APP = Path(__file__).resolve().parent  # ...\Paintbooth\app
ROOT = APP.parent  # ...\Paintbooth
EXE = ROOT / "Paintbooth.exe"
PYW = ROOT / "runtime" / "pythonw.exe"
ICON = APP / "paintbooth.ico"
NAME = "Paintbooth"
REPO_URL = "https://github.com/AL3X-7777/paintbooth"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PaintBooth"
PROGID = "PaintBooth.Project"
# The same program under its earlier names. "Paint booth" (0.4 to 0.10) used the same registry keys
# as today, so only its files, shortcuts and "Open with" entry carry the old spelling.
SPACED_NAME = "Paint booth"
OLD_NAME = "Skinyard"
OLD_LINK_NAMES = (SPACED_NAME, OLD_NAME)
OLD_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Skinyard"
OLD_PROGID = "Skinyard.Project"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
LOG_NAME = "PaintBoothSetup.log"

log = logging.getLogger(__name__)

sys.path.insert(0, str(APP))


# ---------------------------------------------------------------- pure helpers (testable anywhere)


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    r"""Compare two Windows paths the way Windows does.

    Case-insensitive, ``/`` and ``\`` equivalent, and trailing separators ignored, so
    ``C:\Programs\Paintbooth\`` equals ``c:/programs/paintbooth``. Purely textual: it does
    not resolve links, so it also works for folders that no longer exist.
    """

    def norm(p: str | os.PathLike[str]) -> str:
        s = str(PureWindowsPath(os.fspath(p)))
        return s.rstrip("\\").casefold() if len(s) > 3 else s.casefold()

    return norm(a) == norm(b)


def uninstall_values(
    root: Path, app: Path, exe: Path, pyw: Path, icon: Path, version: str, size_kb: int
) -> dict[str, str | int]:
    """Compose the values of the "Apps and features" uninstall entry.

    Args:
        root: The install folder (``InstallLocation``).
        app: The ``app`` folder holding ``winsetup.py``.
        exe: ``Paintbooth.exe`` (unused today; kept so callers pass the full layout).
        pyw: The bundled ``pythonw.exe`` that runs the uninstaller.
        icon: The icon shown in the list.
        version: ``DisplayVersion``; may be empty when the package could not be imported.
        size_kb: ``EstimatedSize`` in KiB.

    Returns:
        Value name to data. ``int`` values are written as ``REG_DWORD``, ``str`` as ``REG_SZ``.
    """
    del exe
    return {
        "DisplayName": NAME,
        "DisplayVersion": version,
        "Publisher": "AL3X",
        "URLInfoAbout": REPO_URL,
        "HelpLink": REPO_URL,
        "URLUpdateInfo": REPO_URL + "/releases",
        "Comments": "Truck and trailer skin mods for ETS2 and ATS",
        "DisplayIcon": str(icon),
        "InstallLocation": str(root),
        "UninstallString": f'"{pyw}" "{app / "winsetup.py"}" uninstall',
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": int(size_kb),
    }


def open_command(exe: Path) -> str:
    r"""Return the ``shell\open\command`` value that opens a file in Paintbooth."""
    return f'"{exe}" "%1"'


def shortcut_targets(start_menu: Path, desktop: Path, want_desktop: bool) -> list[Path]:
    """Decide which shortcuts to (re)create.

    The Start menu entry is always made. The desktop icon is made when the user ticked the box,
    or when one already exists: an update keeps it and points it at the new location.
    """
    links = [start_menu]
    if want_desktop or desktop.exists():
        links.append(desktop)
    return links


def old_install_folders(new_root: Path, recorded: Path | None) -> list[Path]:
    """Return the folders that may hold an old *Skinyard* install, without duplicates.

    Args:
        new_root: The folder Paintbooth is being installed to.
        recorded: ``InstallLocation`` from the old Skinyard uninstall entry, if there was one.

    Returns:
        ``Skinyard`` next to the new folder (the default location), then the recorded folder.
        The new install folder itself is never returned.
    """
    out: list[Path] = []
    for cand in (new_root.parent / OLD_NAME, recorded):
        if cand is None or same_path(cand, new_root) or any(same_path(cand, o) for o in out):
            continue
        out.append(cand)
    return out


def is_program_folder(folder: Path) -> bool:
    """True if ``folder`` looks like a Paintbooth/Skinyard install (it has a ``runtime`` folder).

    This guard keeps the removal code from ever deleting a folder the installer did not create.
    """
    return folder.is_dir() and (folder / "runtime").is_dir()


def remove_program_files(root: Path) -> None:
    """Delete only what the installer put in ``root``, then ``root`` itself if that left it empty.

    The folder the user chose may hold other files, so it is never removed recursively.
    """
    for name in ("runtime", "app"):
        shutil.rmtree(root / name, ignore_errors=True)
    for exe in (NAME, SPACED_NAME):  # the launcher, under today's and the earlier name
        with suppress(FileNotFoundError):
            (root / f"{exe}.exe").unlink()
    with suppress(OSError):  # not empty (user files) or already gone: both fine
        root.rmdir()


def uninstall_script(root: PurePath) -> str:
    r"""Return the batch file that deletes the program files after this process has exited.

    The files are in use by this very process, so the script retries for about 15 s (``ping`` is
    the classic one-second sleep). Only Paintbooth's own files are removed, then the folder if
    that left it empty. At the end the script deletes itself.

    The folder is written into the script, not into a ``cmd`` command line: a batch file has an
    escape for a literal percent sign (``%%``), a command line has none, so a folder such as
    ``D:\100%sure`` would be mangled there. A Windows folder name can't contain ``"``, and inside
    the quotes ``&``, ``^`` and brackets are plain text. ``chcp 65001`` makes ``cmd`` read the
    rest of the file as UTF-8, for folders with non-ASCII names.

    Args:
        root: The install folder.
    """
    r = str(root).rstrip("\\/").replace("%", "%%")
    return "\r\n".join(
        [
            "@echo off",
            "chcp 65001 >nul",
            "for /l %%i in (1,1,15) do (",
            "  ping -n 2 127.0.0.1 >nul",
            f'  if exist "{r}\\runtime" rmdir /s /q "{r}\\runtime"',
            f'  if exist "{r}\\app" rmdir /s /q "{r}\\app"',
            f'  if exist "{r}\\{NAME}.exe" del /f /q "{r}\\{NAME}.exe"',
            f'  if not exist "{r}\\runtime" if not exist "{r}\\app" goto done',
            ")",
            ":done",
            f'rmdir "{r}" 2>nul',  # the folder itself: only if empty (no /s)
            '(goto) 2>nul & del "%~f0"',  # delete this script; (goto) lets cmd release the file first
            "",
        ]
    )


def start_uninstall_script(root: PurePath) -> None:
    """Write :func:`uninstall_script` to the temp folder and start it, detached and without a window.

    The script is started by its bare file name from its own folder, so no path is ever part of
    the ``cmd`` command line.

    Raises:
        OSError: The script could not be written or started.
    """
    folder = Path(tempfile.gettempdir())
    script = folder / f"paintbooth_uninstall_{os.getpid()}.bat"
    script.write_bytes(uninstall_script(root).encode("utf-8"))
    subprocess.Popen(  # fixed command line; the folder is inside the script
        ["cmd", "/d", "/c", script.name],
        creationflags=NO_WINDOW,
        cwd=str(folder),
        close_fds=True,
    )


def ps_quote(p: object) -> str:
    """Quote a value as a PowerShell single-quoted string (``'`` is doubled)."""
    return "'" + str(p).replace("'", "''") + "'"


_q = ps_quote  # short alias used in the PowerShell script lines


def shortcut_script(links: list[Path], exe: Path, root: Path, icon: Path) -> str:
    """Return the PowerShell script that creates ``links`` pointing at ``exe``."""
    lines = ["$s = New-Object -ComObject WScript.Shell"]
    for lnk in links:
        lines += [
            f"$l = $s.CreateShortcut({_q(lnk)})",
            f"$l.TargetPath = {_q(exe)}",
            f"$l.WorkingDirectory = {_q(root)}",
            f"$l.IconLocation = {_q(str(icon) + ',0')}",
            "$l.Description = 'Paintbooth - truck skin mod builder'",
            "$l.Save()",
        ]
    return "; ".join(lines)


def setup_logging(folder: str | None = None) -> Path:
    """Send this module's log records to ``PaintBoothSetup.log`` in ``folder`` (default: %TEMP%).

    Returns:
        The log file path.
    """
    path = Path(folder or tempfile.gettempdir()) / LOG_NAME
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


# ---------------------------------------------------------------- Windows-only parts


def _version() -> str:
    """Return the installed package version, or "" if it cannot be imported."""
    try:
        from paintbooth import __version__
    except ImportError:
        log.exception("could not read the version")
        return ""
    return __version__


def _windll() -> Any:
    """Return ``ctypes.windll`` (typed loosely so this module type-checks on any OS)."""
    import ctypes

    return getattr(ctypes, "windll")  # noqa: B009  (attribute is Windows-only)


def _winreg() -> Any:
    """Import and return ``winreg`` (only available on Windows)."""
    import importlib

    return importlib.import_module("winreg")


def _shell_folder(csidl: int) -> Path:
    """Return a shell folder of the current user (``SHGetFolderPathW``)."""
    import ctypes

    buf = ctypes.create_unicode_buffer(260)  # MAX_PATH
    _windll().shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def _start_menu_link() -> Path:
    """Start menu shortcut path (``CSIDL_PROGRAMS``, this user)."""
    return _shell_folder(0x0002) / f"{NAME}.lnk"


def _desktop_link() -> Path:
    """Desktop shortcut path (``CSIDL_DESKTOPDIRECTORY``)."""
    return _shell_folder(0x0010) / f"{NAME}.lnk"


def _shortcut_paths() -> list[Path]:
    """Both shortcut paths, whether or not they exist."""
    return [_start_menu_link(), _desktop_link()]


def _ps(script: str) -> None:
    """Run a PowerShell script without a window; failures are detected by the caller."""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        creationflags=NO_WINDOW,
        check=False,
        timeout=60,
    )


def make_shortcuts(desktop: bool = False) -> None:
    """Create the Start menu entry, and the desktop icon per :func:`shortcut_targets`.

    PowerShell is tried first; if it is blocked or missing, the shortcuts that did not appear
    are made with a VBScript through ``cscript``.
    """
    links = shortcut_targets(_start_menu_link(), _desktop_link(), desktop)
    for lnk in links:
        lnk.parent.mkdir(parents=True, exist_ok=True)
    _ps(shortcut_script(links, EXE, ROOT, ICON))
    missing = [lnk for lnk in links if not lnk.exists()]
    if missing:
        log.warning("PowerShell did not create %s; trying VBScript", missing)
        vbs = ['Set s = CreateObject("WScript.Shell")']
        for lnk in missing:
            vbs += [
                f'Set l = s.CreateShortcut("{lnk}")',
                f'l.TargetPath = "{EXE}"',
                f'l.WorkingDirectory = "{ROOT}"',
                f'l.IconLocation = "{ICON},0"',
                "l.Save",
            ]
        f = Path(tempfile.gettempdir()) / "paintbooth_shortcuts.vbs"
        f.write_text("\r\n".join(vbs), encoding="mbcs" if os.name == "nt" else "utf-8")
        subprocess.run(["cscript", "//nologo", str(f)], creationflags=NO_WINDOW, check=False, timeout=60)
        f.unlink(missing_ok=True)


def _set(root: int, path: str, values: dict[str, str | int]) -> None:
    """Create ``path`` under ``root`` and write ``values`` (int -> REG_DWORD, str -> REG_SZ)."""
    winreg = _winreg()
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE) as k:
        for name, val in values.items():
            kind = winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ
            winreg.SetValueEx(k, name, 0, kind, val)


def _delete_tree(root: int, path: str) -> None:
    """Delete a registry key and all its subkeys; a missing key is not an error."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)  # always index 0: the previous one was deleted
                except OSError:
                    break
                _delete_tree(root, path + "\\" + sub)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass  # already gone


def _query(root: int, path: str, name: str) -> str | None:
    """Read a string value, or None if the key or value does not exist."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(root, path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return None


def _delete_value(root: int, path: str, name: str) -> None:
    """Delete one value; a missing key or value is not an error."""
    winreg = _winreg()
    with suppress(OSError), winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.DeleteValue(k, name)


def _notify_assoc() -> None:
    """Tell Explorer that file associations changed (``SHCNE_ASSOCCHANGED``)."""
    try:
        _windll().shell32.SHChangeNotify(0x08000000, 0, None, None)
    except (AttributeError, OSError):
        log.exception("SHChangeNotify failed")  # icons refresh at the next logon instead


def register() -> None:
    """Write the uninstall entry, the ``.pb`` file type and "Open with" for ``.pb`` and ``.ms2``."""
    winreg = _winreg()
    hkcu = winreg.HKEY_CURRENT_USER
    size_kb = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file()) // 1024
    _set(hkcu, UNINSTALL_KEY, uninstall_values(ROOT, APP, EXE, PYW, ICON, _version(), size_kb))
    _set(hkcu, rf"Software\Classes\{PROGID}", {"": "Paintbooth project"})
    _set(hkcu, rf"Software\Classes\{PROGID}\DefaultIcon", {"": str(ICON)})
    _set(hkcu, rf"Software\Classes\{PROGID}\shell\open\command", {"": open_command(EXE)})
    # .pb files open in Paintbooth, unless another program already owns .pb on this PC
    if (_query(hkcu, r"Software\Classes\.pb", "") or "") in ("", PROGID):
        _set(hkcu, r"Software\Classes\.pb", {"": PROGID})
    _set(hkcu, r"Software\Classes\.pb\OpenWithProgids", {PROGID: ""})
    # "Open with > Paintbooth" for .ms2 files, without taking over Mods Studio 2's own association
    _set(hkcu, r"Software\Classes\.ms2\OpenWithProgids", {PROGID: ""})
    _set(hkcu, r"Software\Classes\Applications\Paintbooth.exe\shell\open\command", {"": open_command(EXE)})
    _set(hkcu, r"Software\Classes\Applications\Paintbooth.exe\SupportedTypes", {".pb": "", ".ms2": ""})
    _notify_assoc()


def unregister() -> None:
    """Remove everything :func:`register` wrote; ``.pb`` only if it still points at Paintbooth."""
    winreg = _winreg()
    hkcu = winreg.HKEY_CURRENT_USER
    _delete_tree(hkcu, UNINSTALL_KEY)
    _delete_tree(hkcu, rf"Software\Classes\{PROGID}")
    _delete_tree(hkcu, r"Software\Classes\Applications\Paintbooth.exe")
    for ext in (".ms2", ".pb"):
        _delete_value(hkcu, rf"Software\Classes\{ext}\OpenWithProgids", PROGID)
    if _query(hkcu, r"Software\Classes\.pb", "") == PROGID:
        _delete_tree(hkcu, r"Software\Classes\.pb")
    _notify_assoc()


def remove_old_install() -> None:
    r"""Remove the program from when it was still called Skinyard: shortcuts, registry, files.

    The old Documents folder is not touched here (Paintbooth moves it to Documents\Paintbooth).
    The old folder is looked for next to the new one and where the old uninstall entry said, and
    is only deleted if it really holds a program (:func:`is_program_folder`).
    """
    winreg = _winreg()
    hkcu = winreg.HKEY_CURRENT_USER
    for lnk in _shortcut_paths():
        for old in OLD_LINK_NAMES:
            with suppress(FileNotFoundError):
                lnk.with_name(f"{old}.lnk").unlink()
    recorded = _query(hkcu, OLD_UNINSTALL_KEY, "InstallLocation")  # read before deleting the key
    _delete_tree(hkcu, OLD_UNINSTALL_KEY)
    _delete_tree(hkcu, rf"Software\Classes\{OLD_PROGID}")
    _delete_tree(hkcu, r"Software\Classes\Applications\Skinyard.exe")
    _delete_tree(hkcu, rf"Software\Classes\Applications\{SPACED_NAME}.exe")
    _delete_value(hkcu, r"Software\Classes\.ms2\OpenWithProgids", OLD_PROGID)
    for folder in old_install_folders(ROOT, Path(recorded) if recorded else None):
        if is_program_folder(folder):
            log.info("removing the old Skinyard install in %s", folder)
            try:
                shutil.rmtree(folder)
            except OSError:
                log.exception("could not remove all of %s", folder)
                shutil.rmtree(folder, ignore_errors=True)  # remove what can be removed


def _previous_location() -> Path | None:
    """``InstallLocation`` of the current uninstall entry, i.e. where the previous version lives."""
    loc = _query(_winreg().HKEY_CURRENT_USER, UNINSTALL_KEY, "InstallLocation")
    return Path(loc) if loc else None


def _step(what: str, fn: Any, *args: Any) -> None:
    """Run one optional install step; a failure is logged and the install carries on."""
    try:
        fn(*args)
    except Exception:  # an optional step must never stop the install; it is logged
        log.exception("install step failed: %s", what)


def _ensure_documents() -> None:
    r"""Create Documents\Paintbooth (and move an old Documents\Skinyard there)."""
    from paintbooth import appdirs

    appdirs.ensure()


def had_desktop_icon(desktop_link: Path) -> bool:
    """True if a desktop icon exists under the current or an earlier program name.

    Checked before the old shortcuts are removed, so an update keeps the user's desktop icon
    even though its file name changes.
    """
    return any(desktop_link.with_name(f"{n}.lnk").exists() for n in (NAME, *OLD_LINK_NAMES))


def _remove_previous_location() -> None:
    """Installed to a new folder: remove the program files from the previous one."""
    prev = _previous_location()
    if prev is not None and not same_path(prev, ROOT) and is_program_folder(prev):
        log.info("removing the previous install in %s", prev)
        remove_program_files(prev)


def install() -> int:
    """Run the post-install steps (see the module docstring). Returns the process exit code."""
    log.info("installing Paintbooth %s into %s", _version(), ROOT)
    desktop = "--desktop" in sys.argv
    try:
        desktop = desktop or had_desktop_icon(_desktop_link())
    except (OSError, AttributeError):  # no shell or no desktop folder: nothing to keep
        log.exception("could not look for an existing desktop icon")
    _step("create Documents\\Paintbooth", _ensure_documents)
    _step("remove the old Skinyard install", remove_old_install)
    _step("remove the previous install folder", _remove_previous_location)
    code = 0
    for what, fn, args in (("create the shortcuts", make_shortcuts, (desktop,)), ("register", register, ())):
        try:
            fn(*args)
        except Exception:  # each step is tried even if the other failed; Setup.exe reports code 1
            log.exception("install step failed: %s", what)
            code = 1
    log.info("install finished" if code == 0 else "install finished with errors")
    return code


def _ask(text: str) -> bool:
    """Ask a Yes/No question (MB_YESNO | MB_ICONQUESTION); True for Yes (IDYES = 6)."""
    return bool(_windll().user32.MessageBoxW(None, text, "Uninstall Paintbooth", 0x04 | 0x20) == 6)


def uninstall() -> int:
    """Remove shortcuts, registry entries and program files. Returns the process exit code."""
    docs = ""
    try:
        from paintbooth import appdirs

        docs = str(appdirs.home())
    except (ImportError, OSError):
        log.exception("could not find the Documents folder")  # only used in the question text
    if "/S" not in sys.argv and not _ask(
        f"Remove Paintbooth from this computer?\n\nYour projects and settings in\n{docs}\nwill be kept."
    ):
        return 1
    log.info("uninstalling Paintbooth from %s", ROOT)
    for lnk in _shortcut_paths():
        with suppress(FileNotFoundError):
            lnk.unlink()
    unregister()
    try:
        start_uninstall_script(ROOT)
    except OSError:
        log.exception("could not start the clean-up script; the program files stay in %s", ROOT)
        return 1
    return 0


def main(argv: list[str]) -> int:
    """Dispatch ``install`` (default) or ``uninstall``; any other word returns 2."""
    what = argv[1] if len(argv) > 1 else "install"
    with suppress(OSError):  # no writable %TEMP%: run without a log
        setup_logging()
    if what == "install":
        return install()
    if what == "uninstall":
        return uninstall()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
