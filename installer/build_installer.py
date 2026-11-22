r"""Build the Paintbooth installer (``Paintbooth Setup X.Y.Z.exe``) and the portable zip.

Runs on Linux or Windows. CI (``.github/workflows/release.yml``) runs it on a Windows runner and
passes the toolcache Python that ``actions/setup-python`` installed; locally any Windows CPython
3.13 x64 folder works (for example a python-build-standalone ``x86_64-pc-windows-msvc``
``install_only`` build unpacked on Linux).

Needs:
    * ``pip install ziglang`` - the C cross-compiler (``python -m ziglang cc``);
    * a Windows CPython 3.13 x64 folder (``python.exe``, ``DLLs``, ``Lib``, ``tcl``);
    * ``installer/miniz/miniz.c`` and ``miniz.h`` (single-file amalgamation, in the repository);
    * the cp313 Pillow and numpy wheels in ``wheels/``. They are not kept in git; their pinned
      versions and SHA-256 hashes are in ``wheels/requirements-offline.txt`` and are checked here
      before anything is unpacked.

Usage::

    python installer/build_installer.py --python WINPY --out DIST [--source-zip]

Output layout
-------------
``Setup.exe`` is the compiled ``setup.c`` stub with a ZIP payload appended, followed by a 16-byte
trailer::

    [ PE stub ][ ZIP of the "Paintbooth" folder ][ b"SKYPAYLD" ][ uint64 LE offset of the ZIP ]

After Authenticode signing, the certificate table is appended *behind* the trailer (8-byte
aligned), so the stub also looks for the trailer just before that table; see
:func:`find_payload`, which mirrors ``trailer_end`` in ``setup.c``. The payload folder is::

    Paintbooth.exe        launcher.c (starts runtime\pythonw.exe with app\Paintbooth.pyw)
    runtime\              trimmed CPython with Pillow and numpy in Lib\site-packages
    app\                  the paintbooth package, Paintbooth.pyw, winsetup.py, icon, licence

The portable zip holds the same folder under ``Paintbooth/``. With ``--source-zip`` a source
archive (git-tracked files plus the offline wheels, under ``paintbooth/``) is written as well.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WHEELS = REPO / "wheels"
OFFLINE_REQUIREMENTS = WHEELS / "requirements-offline.txt"
sys.path.insert(0, str(REPO))
from paintbooth import __version__  # noqa: E402  (needs the sys.path entry above)

TRAILER_MAGIC = b"SKYPAYLD"
TRAILER_SIZE = 16  # magic (8) + uint64 little-endian payload offset (8)
BUNDLED_WHEEL_TAG = "cp313-cp313-win_amd64"  # matches the Python runtime that is bundled
#: tkinterdnd2 (dragging files onto the window) is pure Python plus the tkdnd binaries.
BUNDLED_PACKAGES = ("pillow", "numpy", "tkinterdnd2")
WHEEL_TAGS = {"tkinterdnd2": "py3-none-any"}

# Parts of the Windows Python runtime that Paintbooth never uses (keeps the installer small).
DROP_DIRS = {"include", "libs", "Scripts", "share"}
DROP_LIB = {
    "test",
    "idlelib",
    "ensurepip",
    "turtledemo",
    "lib2to3",
    "pydoc_data",
    "__pycache__",
    "site-packages",
}
DROP_DLL_PREFIX = ("_test", "xxlimited", "_ctypes_test")
DROP_TCL = {"demos", "images"}  # per tcl/tk version folder; tk's msgs are kept for dialogs


class BuildError(RuntimeError):
    """A build input is missing or does not match its pinned hash."""


# ---------------------------------------------------------------- offline wheels


def read_requirements(path: Path) -> dict[str, tuple[str, set[str]]]:
    r"""Parse a pip ``--require-hashes`` requirements file.

    Only the subset this project writes is understood: ``name==version`` followed by one or more
    ``--hash=sha256:<hex>`` options, with ``\`` line continuations and ``#`` comments.

    Args:
        path: The requirements file, normally ``wheels/requirements-offline.txt``.

    Returns:
        ``{lower-case name: (version, {sha256 hex, ...})}``.

    Raises:
        BuildError: A requirement is not pinned with ``==`` or has no hash.
    """
    text = path.read_text(encoding="utf-8")
    logical = re.sub(r"\\\r?\n", " ", text)  # join continuation lines
    reqs: dict[str, tuple[str, set[str]]] = {}
    for raw in logical.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, *opts = line.split()
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.+\-]+)", spec)
        if not m:
            raise BuildError(f"{path.name}: {spec!r} is not pinned as name==version")
        hashes = {o.split(":", 1)[1].lower() for o in opts if o.startswith("--hash=sha256:")}
        if not hashes:
            raise BuildError(f"{path.name}: {spec} has no --hash=sha256 entry")
        reqs[m.group(1).lower()] = (m.group(2), hashes)
    return reqs


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a file as lower-case hex, reading it in 1 MB blocks."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_wheel(wheel: Path, reqs: dict[str, tuple[str, set[str]]]) -> None:
    """Check that a wheel is pinned in ``reqs`` and that its SHA-256 is one of the listed hashes.

    Args:
        wheel: Path of a ``.whl`` file whose name starts with ``<name>-<version>-``.
        reqs: Parsed requirements from :func:`read_requirements`.

    Raises:
        BuildError: The wheel is missing, unpinned, the wrong version, or its hash is not listed.
    """
    if not wheel.is_file():
        raise BuildError(
            f"{wheel} is missing. The wheels are not kept in git; fetch them with the "
            f"'pip download' command at the top of {OFFLINE_REQUIREMENTS.name}."
        )
    name, version = wheel.name.split("-")[:2]
    pinned = reqs.get(name.lower())
    if pinned is None or pinned[0] != version:
        raise BuildError(f"{wheel.name} is not pinned in {OFFLINE_REQUIREMENTS.name}")
    if sha256_file(wheel) not in pinned[1]:
        raise BuildError(f"{wheel.name}: SHA-256 does not match {OFFLINE_REQUIREMENTS.name}")


def bundled_wheels(reqs: dict[str, tuple[str, set[str]]]) -> list[Path]:
    """Return the verified cp313 wheels that go into the bundled runtime.

    The file names are derived from the pinned versions, so a version bump only has to be made
    in ``wheels/requirements-offline.txt``.

    Raises:
        BuildError: See :func:`verify_wheel`, or a bundled package is not listed at all.
    """
    out = []
    for name in BUNDLED_PACKAGES:
        if name not in reqs:
            raise BuildError(f"{name} is not listed in {OFFLINE_REQUIREMENTS.name}")
        wheel = WHEELS / f"{name}-{reqs[name][0]}-{WHEEL_TAGS.get(name, BUNDLED_WHEEL_TAG)}.whl"
        verify_wheel(wheel, reqs)
        out.append(wheel)
    return out


# ---------------------------------------------------------------- payload trailer


def build_setup(stub: bytes, payload: bytes) -> bytes:
    """Return ``stub + payload + trailer``, the layout ``setup.c`` reads back."""
    return stub + payload + TRAILER_MAGIC + struct.pack("<Q", len(stub))


def _trailer_end(exe: bytes) -> int:
    """Return the offset just past the trailer, or 0 if none is found (mirror of ``trailer_end``).

    Unsigned: the trailer is the last 16 bytes. Signed: the PE security directory (data directory
    entry 4) gives the file offset of the certificate table, which signing appended after the
    trailer; the trailer ends at most 32 bytes before it (8-byte alignment padding).
    """
    n = len(exe)
    if n >= TRAILER_SIZE and exe[n - TRAILER_SIZE : n - 8] == TRAILER_MAGIC:
        return n
    if n < 0x40:
        return 0
    pe = struct.unpack_from("<I", exe, 0x3C)[0]
    if pe + 24 + 2 > n or exe[pe : pe + 4] != b"PE\0\0":
        return 0
    opt = pe + 24  # optional header: after the 4-byte signature and 20-byte COFF header
    magic = struct.unpack_from("<H", exe, opt)[0]
    sec_dir = opt + (112 if magic == 0x20B else 96) + 4 * 8  # PE32+ / PE32, IMAGE_DIRECTORY_ENTRY_SECURITY
    if sec_dir + 8 > n:
        return 0
    cert = struct.unpack_from("<I", exe, sec_dir)[0]
    if cert < TRAILER_SIZE or cert > n:
        return 0
    end = cert
    while end >= TRAILER_SIZE and end + 32 >= cert:
        if exe[end - TRAILER_SIZE : end - 8] == TRAILER_MAGIC:
            return end
        end -= 1
    return 0


def find_payload(exe: bytes) -> tuple[int, int]:
    """Locate the ZIP payload of a built (and possibly signed) Setup.exe.

    This is the Python twin of ``extract_payload``'s first half in ``setup.c``; the build uses
    it to check its own output, and the tests use it to pin the layout.

    Args:
        exe: The whole installer file.

    Returns:
        ``(offset, size)`` of the ZIP inside ``exe``.

    Raises:
        BuildError: No trailer, or its offset points outside the file.
    """
    end = _trailer_end(exe)
    if end < TRAILER_SIZE:
        raise BuildError("no SKYPAYLD trailer found")
    off = struct.unpack_from("<Q", exe, end - 8)[0]
    if off >= end - TRAILER_SIZE:
        raise BuildError("SKYPAYLD trailer points outside the file")
    return off, end - TRAILER_SIZE - off


# ---------------------------------------------------------------- compiling


def zig(*args: str, cwd: Path) -> None:
    """Run ``zig cc`` for 64-bit Windows (MinGW ABI) with the given arguments."""
    cmd = [sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd)


def rc_file(path: Path, ico: Path, desc: str, name: str) -> None:
    """Write a Windows resource script with the icon and the version information block.

    Args:
        path: The ``.rc`` file to write (next to ``ico``; the icon is referenced by file name).
        ico: The icon file.
        desc: ``FileDescription`` (shown in Task Manager and the file's Properties).
        name: ``OriginalFilename``.
    """
    v = [*__version__.split("."), "0", "0", "0", "0"]
    nums = ",".join(v[:4])  # FILEVERSION needs exactly four numbers
    path.write_text(f'''1 ICON "{ico.name}"
1 VERSIONINFO
FILEVERSION {nums}
PRODUCTVERSION {nums}
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904b0"
    BEGIN
      VALUE "CompanyName", "AL3X"
      VALUE "FileDescription", "{desc}"
      VALUE "FileVersion", "{__version__}"
      VALUE "ProductName", "Paintbooth"
      VALUE "ProductVersion", "{__version__}"
      VALUE "OriginalFilename", "{name}"
      VALUE "LegalCopyright", "Copyright (c) 2026 AL3X. MIT licence."
      VALUE "Comments", "https://github.com/AL3X-7777/paintbooth"
      VALUE "InternalName", "Paintbooth"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
''')


def compile_launcher(work: Path, ico: Path) -> Path:
    """Compile ``launcher.c`` into ``Paintbooth.exe`` (GUI subsystem, no console)."""
    rc_file(work / "launcher.rc", ico, "Paintbooth", "Paintbooth.exe")
    launcher = work / "Paintbooth.exe"
    zig(
        "-O2",
        "-municode",
        "-Wl,--subsystem,windows",
        str(HERE / "launcher.c"),
        str(work / "launcher.rc"),
        "-o",
        str(launcher),
        "-luser32",
        "-lkernel32",
        cwd=work,
    )
    return launcher


def compile_setup_stub(work: Path, ico: Path, miniz: Path) -> Path:
    """Compile ``setup.c`` with miniz into the installer stub (payload is appended later)."""
    rc_file(work / "setup.rc", ico, "Paintbooth Setup", f"Paintbooth Setup {__version__}.exe")
    stub = work / "setup_stub.exe"
    zig(
        "-O2",
        "-municode",
        "-Wl,--subsystem,windows",
        f'-DSKY_VERSION=L"{__version__}"',
        "-DMINIZ_NO_STDIO",
        "-DMINIZ_NO_ZLIB_COMPATIBLE_NAMES",
        f"-I{miniz}",
        str(HERE / "setup.c"),
        str(miniz / "miniz.c"),
        str(work / "setup.rc"),
        "-o",
        str(stub),
        "-luser32",
        "-lkernel32",
        "-lshell32",
        "-lgdi32",
        "-lole32",
        "-ladvapi32",
        cwd=work,
    )
    return stub


# ---------------------------------------------------------------- staging


def stage_runtime(winpy: Path, dest: Path) -> None:
    """Copy the Windows Python runtime to ``dest`` without the parts Paintbooth never uses.

    ``python.exe`` is kept on purpose: it is handy for troubleshooting from a console.
    """
    shutil.copytree(winpy, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for d in DROP_DIRS:
        shutil.rmtree(dest / d, ignore_errors=True)
    for d in DROP_LIB:
        shutil.rmtree(dest / "Lib" / d, ignore_errors=True)
    (dest / "Lib" / "site-packages").mkdir()
    for f in (dest / "DLLs").glob("*"):
        if f.name.startswith(DROP_DLL_PREFIX):
            f.unlink()
    for sub in (dest / "tcl").glob("*"):
        if sub.is_dir():
            for d in DROP_TCL:
                shutil.rmtree(sub / d, ignore_errors=True)


def add_wheel(wheel: Path, site: Path) -> None:
    """Unpack a (hash-verified) wheel into ``site-packages``; a wheel is a plain ZIP."""
    with zipfile.ZipFile(wheel) as z:
        z.extractall(site)


def stage_app(dest: Path, ico: Path) -> None:
    """Copy the application files (package, start script, post-install script, icon) to ``dest``."""
    dest.mkdir(parents=True)
    shutil.copytree(
        REPO / "paintbooth", dest / "paintbooth", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for f in ("Paintbooth.pyw", "LICENSE", "README.md"):
        shutil.copy2(REPO / f, dest / f)
    shutil.copy2(HERE / "winsetup.py", dest / "winsetup.py")
    shutil.copy2(ico, dest / "paintbooth.ico")


def zip_dir(src: Path) -> bytes:
    """Return a deflated ZIP of everything under ``src`` (sorted, POSIX names relative to it)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())
    return buf.getvalue()


def write_portable_zip(stage: Path, port: Path) -> None:
    """Write the portable zip: the staged folder under ``Paintbooth/``."""
    with zipfile.ZipFile(port, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, "Paintbooth/" + f.relative_to(stage).as_posix())


def source_files() -> list[str]:
    """List the files for the source zip: git's files (tracked or new, not ignored) plus wheels/.

    The offline wheels are ignored by git but belong in the source zip, so ``install.bat`` can
    install Pillow and numpy without internet access.
    """
    res = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    files = {f for f in res.stdout.splitlines() if (REPO / f).is_file()}
    files |= {f"wheels/{w.name}" for w in WHEELS.glob("*.whl")}
    return sorted(files)


def write_source_zip(dest: Path, reqs: dict[str, tuple[str, set[str]]]) -> None:
    """Write the source archive under ``paintbooth/``, after verifying every bundled wheel.

    Raises:
        BuildError: A wheel in ``wheels/`` does not match ``requirements-offline.txt``.
    """
    files = source_files()
    for f in files:
        if f.startswith("wheels/") and f.endswith(".whl"):
            verify_wheel(REPO / f, reqs)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(REPO / f, "paintbooth/" + f)


# ---------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    ap = argparse.ArgumentParser(description="Build the Paintbooth installer and portable zip.")
    ap.add_argument(
        "--python",
        required=True,
        type=Path,
        help="a Windows CPython 3.13 x64 folder (python.exe, DLLs, Lib, tcl)",
    )
    ap.add_argument("--miniz", type=Path, default=HERE / "miniz", help="folder with miniz.c / miniz.h")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--work", type=Path)
    ap.add_argument(
        "--source-zip", action="store_true", help="also write the source zip (with the offline wheels)"
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Build Setup.exe and the portable zip (and the source zip with ``--source-zip``)."""
    a = parse_args(argv)
    # absolute: the compiler runs with the work folder as its current directory
    out: Path = a.out.resolve()
    work: Path = a.work.resolve() if a.work else out / "_work"
    reqs = read_requirements(OFFLINE_REQUIREMENTS)
    wheels = bundled_wheels(reqs)  # fail early, before compiling anything
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)

    ico = work / "paintbooth.ico"
    subprocess.run([sys.executable, str(HERE / "make_icon.py"), str(ico)], check=True)
    launcher = compile_launcher(work, ico)
    stub = compile_setup_stub(work, ico, a.miniz)

    stage = work / "Paintbooth"
    stage.mkdir()
    stage_runtime(a.python, stage / "runtime")
    for wheel in wheels:
        add_wheel(wheel, stage / "runtime" / "Lib" / "site-packages")
    stage_app(stage / "app", ico)
    shutil.copy2(launcher, stage / "Paintbooth.exe")

    payload = zip_dir(stage)
    exe = build_setup(stub.read_bytes(), payload)
    off, size = find_payload(exe)  # self-check: the stub must find exactly this payload
    if exe[off : off + size] != payload:
        raise BuildError("payload self-check failed")
    setup = out / f"Paintbooth Setup {__version__}.exe"
    setup.write_bytes(exe)
    print(f"{setup}  {setup.stat().st_size / 1e6:.1f} MB")

    port = out / f"Paintbooth {__version__} portable.zip"  # unzip anywhere, run "Paintbooth.exe"
    write_portable_zip(stage, port)
    print(f"{port}  {port.stat().st_size / 1e6:.1f} MB")

    if a.source_zip:
        src = out / f"Paintbooth {__version__} source.zip"
        write_source_zip(src, reqs)
        print(f"{src}  {src.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    try:
        main()
    except BuildError as e:
        sys.exit(f"build_installer: {e}")
