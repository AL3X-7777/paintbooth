"""Validate and export Mods Studio 2 template items (see :mod:`paintbooth.ms2tpl` for the build engine).

:func:`validate_item` runs the validators a template declares for its variables (the same ones
MS2's editor runs) and checks the files they refer to; :func:`plan_item` runs the item's build
actions and turns the result into :class:`~paintbooth.builder.OutFile` entries for the exporter.

Both need the user's Mods Studio 2 installation. Where it is comes from the app settings
(``ms2_dir``), which the GUI and CLI pass in with :func:`configure`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from . import ms2data
from .builder import OutFile, TextureJob
from .ms2tpl import (
    KNOWN_ACTIONS,
    Engine,
    TemplateError,
    Value,
    array_items,
    child,
    compile_dotnet,
    enabled,
    var_text,
)
from .names import TOKEN_RE
from .util import is_pot
from .xmldom import Elem

if TYPE_CHECKING:
    from .ms2 import Ms2Project, TemplateItem

SCS_NAME_RE = TOKEN_RE  # one token rule for the whole app (paintbooth.names)

# Deprecated: assigning a dict here still works (tests and older callers do it), but callers
# should use configure(). A configured mapping takes precedence.
SETTINGS: dict[str, Any] = {}

# Builds a Problem: (level, item index, field, message). ms2build passes its Problem class in,
# which avoids an import cycle.
ProblemFactory = Callable[[str, int, str, str], Any]
Finder = Callable[[str], "Path | None"]


class _Config:
    """Holder for the configured settings (a class attribute, so no ``global`` is needed)."""

    settings: Mapping[str, Any] | None = None


def configure(settings: Mapping[str, Any] | None) -> None:
    """Tell the template code the app settings (``ms2_dir``: where Mods Studio 2 is installed).

    The mapping is kept by reference, so later changes to it are seen. ``None`` goes back to
    the module-level :data:`SETTINGS`.
    """
    _Config.settings = settings


def _settings() -> Mapping[str, Any]:
    """The configured settings, or :data:`SETTINGS`."""
    return _Config.settings if _Config.settings is not None else SETTINGS


def ms2_dir(project: Ms2Project) -> Path | None:
    """The Mods Studio 2 folder for ``project``: the configured one, or one the project names."""
    return ms2data.find(_settings(), ms2data.project_hints(project.root))


def template_folder(it: TemplateItem, ms2: Path | None) -> Path | None:
    """The item's template folder (``TemplateBaseFolder``) inside ``ms2``, or None."""
    if ms2 is None:
        return None
    return ms2data.template_dir(ms2, it.el.get_text("TemplateBaseFolder"))


def resolver(project: Ms2Project, ms2: Path | None, tdir: Path | None) -> Finder:
    """Make a function that finds a file the item refers to.

    It tries the path as written (or next to the project), then the same path inside this PC's
    MS2 folder (for paths saved on another PC), then the file name in the template folder.
    """
    from .ms2build import resolve

    base = project.path.parent if project.path else None
    hints = ms2data.project_hints(project.root)

    def in_ms2(text: str) -> Path | None:
        """The file inside this PC's MS2 folder, for a path saved with another MS2 folder."""
        if ms2 is None:
            return None
        low = text.replace("/", "\\").lower()
        for h in [*hints, str(ms2), "C:\\Mods Studio 2\\"]:
            hp = h.replace("/", "\\").rstrip("\\").lower() + "\\"
            if low.startswith(hp):
                q = ms2data.ci_path(ms2, text.replace("/", "\\")[len(hp) :])
                if q is not None and q.is_file():
                    return q
        if "\\data\\modprojects\\" in low:
            q = ms2data.ci_path(ms2, text.replace("/", "\\")[low.index("\\data\\modprojects\\") :])
            if q is not None and q.is_file():
                return q
        return None

    def find(text: str) -> Path | None:
        text = (text or "").strip()
        if not text:
            return None
        p = resolve(text, base) or in_ms2(text)
        if p is not None:
            return p
        if tdir is not None:
            q = ms2data.ci_path(tdir, PureWindowsPath(text).name)
            if q is not None and q.is_file():
                return q
        return None

    return find


def default_image(v: Elem, tdir: Path | None) -> Path | None:
    """A template's built-in default for an image variable (e.g. the lightbox shop icon)."""
    opts = v.find("UseDefaultOptions")
    for o in opts.children if opts is not None else []:
        if tdir is not None and o.text:
            q = ms2data.ci_path(tdir, o.text)
            if q is not None and q.is_file():
                return q
    return None


def _actions(el: Elem) -> Iterator[Elem]:
    """Every ``<Action>`` below ``el``, nested ones included."""
    for c in el.children:
        if c.tag == "Action":
            yield c
        yield from _actions(c)


def var_lookup(variables: Iterable[Elem]) -> Callable[[str], Elem | None]:
    """Return a function that finds a variable by its MS2 name.

    ``"Truck"`` names a top-level variable; ``"Truck.Pattern"`` names the child variable ``Pattern``
    of the currently selected ``Truck`` option. Conditions and count limits use these dotted names.
    Shared by validation here and by the template editor in the GUI.

    Args:
        variables: The ``<Variable>`` elements to search (an item's, or one cargo's children).
    """
    vars_ = {v.get_text("Name"): v for v in variables}

    def lookup(name: str) -> Elem | None:
        if name in vars_:
            return vars_[name]
        head, _, rest = name.partition(".")
        v = vars_.get(head)
        for p in rest.split(".") if rest else []:
            v = child(v, p) if v is not None else None
        return v

    return lookup


def _lookup_for(it: TemplateItem) -> Callable[[str], Value | None]:
    """A variable lookup over one template item's variables (see :func:`var_lookup`)."""
    return var_lookup(it.variables())


# ====================================================================== validation


@dataclass
class _Check:
    """Shared state while validating one item."""

    idx: int
    problem: ProblemFactory
    lookup: Callable[[str], Value | None]
    names_seen: dict[str, int]
    probs: list[Any]

    def error(self, field: str, msg: str) -> None:
        """Add an error for ``field``."""
        self.probs.append(self.problem("error", self.idx, field, msg))

    def warning(self, field: str, msg: str) -> None:
        """Add a warning for ``field``."""
        self.probs.append(self.problem("warning", self.idx, field, msg))


@dataclass
class _Var:
    """One variable being validated."""

    el: Elem
    name: str
    title: str
    type: str
    text: str
    items: list[str | Elem] | None  # array items, for array variables
    has_default: bool  # an empty image field falls back to the template default


def _check_count(ck: _Check, v: _Var, vd: Elem) -> None:
    """ItemCountValidator: MinCount, MaxCount or a MaxCountSource variable."""
    if v.items is None:
        return
    lo = vd.get_text("MinCount")
    hi = vd.get_text("MaxCount")
    src = vd.get_text("MaxCountSource")
    if src:
        m = re.fullmatch(r"\[([^\]]+)\]", src.strip())
        sv = ck.lookup(m.group(1)) if m else None
        hi = var_text(sv) if sv is not None else hi
    if lo and len(v.items) < int(lo):
        ck.error(v.name, f"{v.title}: add at least {lo}")
    if hi and hi.isdigit() and len(v.items) > int(hi):
        ck.error(v.name, f"{v.title}: at most {hi} for this choice (you have {len(v.items)})")


def _check_range(ck: _Check, v: _Var, vd: Elem) -> None:
    """NumberRangeValidator: MinValue / MaxValue."""
    try:
        x = float(v.text)
        lo, hi = vd.get_text("MinValue"), vd.get_text("MaxValue")
        if lo and x < float(lo):
            ck.error(v.name, f"{v.title}: must be at least {lo}")
        if hi and x > float(hi):
            ck.error(v.name, f"{v.title}: must be at most {hi}")
    except ValueError:
        ck.error(v.name, f"{v.title}: must be a number")


def _check_regex(ck: _Check, v: _Var, vd: Elem) -> None:
    """RegexValidator: the whole value must match RegexPattern (.NET syntax, translated)."""
    pat = vd.get_text("RegexPattern")
    if not pat:
        return
    try:
        rx = compile_dotnet(pat)
    except TemplateError as e:
        ck.error(v.name, f"{v.title}: the template's format check can't be used ({e})")
        return
    if not rx.fullmatch(v.text):
        ck.error(v.name, f"{v.title}: '{v.text}' is not in the expected format")


def _check_validator(ck: _Check, v: _Var, vd: Elem) -> None:
    """Run one of the variable's ``<Validator>`` elements (file checks are separate)."""
    k = vd.type()
    text, name, title = v.text, v.name, v.title
    if k == "NotEmptyValidator":
        empty = (not v.items) if v.items is not None else not text.strip()
        if empty and not v.has_default:
            ck.error(name, f"{title}: " + (vd.get_text("AdviceMessage") or "required"))
    elif k == "ScsInternalNameValidator" and text:
        if not SCS_NAME_RE.match(text):
            ck.error(name, f"{title}: use 1-12 characters a-z, 0-9 and _")
    elif k == "ScsDuplicatedInternalNameValidator" and text:
        if text in ck.names_seen and ck.names_seen[text] != ck.idx:
            ck.error(name, f"{title}: '{text}' is also used by item {ck.names_seen[text] + 1}")
        else:
            ck.names_seen.setdefault(text, ck.idx)
    elif k == "ScsValidStringValidator" and text and re.search(r'["\r\n]', text):
        ck.error(name, f"{title}: may not contain quotes or line breaks")
    elif k == "NumberRangeValidator" and text.strip():
        _check_range(ck, v, vd)
    elif k == "ItemCountValidator":
        _check_count(ck, v, vd)
    elif k == "RegexValidator" and text:
        _check_regex(ck, v, vd)


def _check_image(ck: _Check, v: _Var, where: str, size: tuple[int, int], validators: list[Elem]) -> None:
    """NPOTValidator and ImageSizeValidator for one image of ``size``."""
    for vd in validators:
        if vd.type() == "NPOTValidator" and not (is_pot(size[0]) and is_pot(size[1])):
            ck.error(
                v.name, f"{where}: {size[0]}x{size[1]} is not a power of two (e.g. 1024x1024, 2048x1024)"
            )
        if vd.type() == "ImageSizeValidator":
            try:
                w, h = int(vd.get_text("Width")), int(vd.get_text("Height"))
            except ValueError:
                continue
            if size != (w, h):
                ck.warning(v.name, f"{where}: {size[0]}x{size[1]} will be resized to {w}x{h}")


def _check_files(ck: _Check, v: _Var, find: Finder, validators: list[Elem]) -> None:
    """The files of image and mod-file variables: do they exist, and are the images right."""
    from .ms2build import image_size

    if v.type in ("ScsImageSourceVariable", "FileSourceVariable"):
        paths = [v.text] if v.text.strip() else []
    elif v.type == "ScsImageSourceArrayVariable":
        paths = [x if isinstance(x, str) else var_text(x) for x in v.items or []]
    else:
        return
    for k, pth in enumerate(paths):
        where = f"{v.title}" + (f" {k + 1}" if len(paths) > 1 else "")
        p = find(pth)
        if p is None:
            ck.error(v.name, f"{where}: file not found ({PureWindowsPath(pth).name})")
            continue
        if v.type == "FileSourceVariable":
            continue
        size = image_size(p)
        if size is None:
            ck.error(v.name, f"{where}: can't read {p.name}")
            continue
        _check_image(ck, v, where, size, validators)


def _check_cargo(ck: _Check, it: TemplateItem, find: Finder) -> None:
    """CargoContainer2: every cargo needs a valid internal name and an existing icon."""
    for c in it.array("CargoList"):
        if not isinstance(c, Elem):
            continue
        cn = var_text(child(c, "CargoInternalName"))
        if not SCS_NAME_RE.match(cn or ""):
            ck.error(
                "CargoList", f"Cargo '{cn or '?'}': internal name must be 1-12 characters a-z, 0-9 and _"
            )
        icon = var_text(child(c, "CargoIcon"))
        if icon and find(icon) is None:
            ck.error("CargoList", f"Cargo '{cn}': icon not found ({PureWindowsPath(icon).name})")


def _setup_problems(ck: _Check, it: TemplateItem, ms2: Path | None, label: str) -> Path | None:
    """Check that MS2 and the template folder exist; returns the folder, or None after an error."""
    if ms2 is None:
        ck.error(
            "",
            f"{label}: Paintbooth needs Mods Studio 2 installed on this PC for this "
            "item (its template files are used). Set the folder in Settings.",
        )
        return None
    tdir = template_folder(it, ms2)
    if tdir is None or not tdir.is_dir():
        ck.error(
            "",
            f"{label}: the Mods Studio 2 template folder "
            f"'{it.el.get_text('TemplateBaseFolder')}' was not found in {ms2}",
        )
        return None
    unknown = sorted({a.type() for a in _actions(it.el) if a.type() not in KNOWN_ACTIONS})
    if unknown:
        ck.error("", f"{label}: uses build steps Paintbooth doesn't know yet: " + ", ".join(unknown))
    return tdir


def validate_item(
    project: Ms2Project,
    idx: int,
    it: TemplateItem,
    names_seen: dict[str, int],
    Problem: ProblemFactory,  # noqa: N803 - the caller passes its Problem class; name kept for callers
) -> list[Any]:
    """Check a template item the way MS2's editor does.

    Args:
        project: The project the item belongs to.
        idx: The item's index (problems are reported against it).
        it: The item.
        names_seen: Internal name -> item index, shared across items to find duplicates.
        Problem: Builds a problem from (level, item index, field, message).

    Returns:
        The problems found; errors block the export.
    """
    ck = _Check(idx, Problem, _lookup_for(it), names_seen, [])
    ms2 = ms2_dir(project)
    label = ms2data.FEATURES.get(it.type, (it.template_name, ""))[0]
    tdir = _setup_problems(ck, it, ms2, label)
    if tdir is None:
        return ck.probs
    find = resolver(project, ms2, tdir)
    for el in it.variables():
        name = el.get_text("Name")
        if not el.get_text("Text") or name.startswith("Env.") or not enabled(el, ck.lookup):
            continue
        t = el.type()
        v = _Var(
            el,
            name,
            el.get_text("Text"),
            t,
            el.get_text("Value"),
            array_items(el) if t.endswith("ArrayVariable") else None,
            default_image(el, tdir) is not None,
        )
        vals = el.find("Validators")
        validators = vals.children if vals is not None else []
        for vd in validators:
            _check_validator(ck, v, vd)
        _check_files(ck, v, find, validators)
    if it.type == "CargoContainer2":
        _check_cargo(ck, it, find)
    return ck.probs


# ====================================================================== export


def plan_item(project: Ms2Project, it: TemplateItem) -> tuple[list[OutFile], list[str]]:
    """Run a template item's build actions into mod files.

    Returns:
        The files (``.tmp`` helper files dropped) and the engine's warnings.

    Raises:
        TemplateError: The template folder is missing or a build action fails.
    """
    ms2 = ms2_dir(project)
    tdir = template_folder(it, ms2)
    if tdir is None:
        raise TemplateError("Mods Studio 2 template folder not found")
    find = resolver(project, ms2, tdir)

    def resolve_file(text: str) -> Path | None:
        p = find(text)
        if p is None and not (text or "").strip():
            return None
        return p

    eng = Engine(it.el, tdir, resolve_file)

    def fallback(raw: str) -> Path | None:
        if "CargoIcon" in raw and ms2 is not None:  # MS2's generic cargo icon
            q = ms2data.ci_path(ms2, "Data/ModData/SCS/Icons/cargoicon.png")
            return q if q is not None and q.is_file() else None
        return None

    eng.fallback_image = fallback
    # empty image fields with a template default (e.g. the lightbox shop icon) use that default
    for v in it.variables():
        if v.type() == "ScsImageSourceVariable" and not v.get_text("Value").strip():
            d = default_image(v, tdir)
            if d is not None:
                eng.scopes[0][v.get_text("Name")] = str(d)
    files = eng.run()
    out = []
    for path, e in files.items():
        if path.endswith(".tmp"):
            continue
        if isinstance(e, bytes):
            out.append(OutFile(path, e))
        elif isinstance(e, Path):
            out.append(OutFile(path, job=TextureJob(e, "copy", None, "RAW")))
        else:
            out.append(OutFile(path, job=e))
    return out, eng.warnings
