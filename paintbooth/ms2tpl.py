"""Run a Mods Studio 2 template item (lightbox, windscreen decal, cargo container, ...) into mod files.

A template (``template.xml`` in the user's MS2 folder, also stored inside every
.ms2/.pb item) declares *variables* the user fills in and *build actions*
that turn them into files: copy files from the template folder, write and
patch text, import images as textures, write .tobj files, rename the
materials of 3D models, loop over lists, and so on.

``{{Name}}`` in a path or text is replaced by a variable's value. ``{{A.B}}``
is the child value B of A (e.g. the selected truck's InternalName), and
``{{INDEX:D2}}`` formats a number with two digits. Names that are not known
(yet) stay in the text untouched - later steps fill them in.

The build actions run are the ones stored in the project item, so a shared
project file is untrusted input. Every path is therefore checked
(:meth:`Engine.norm`, :func:`paintbooth.ms2data.ci_path`): a template can't read
files outside its template folder, and can't name mod files outside the mod.
Regular expressions are .NET syntax and are translated (:func:`dotnet_regex`).

This is an independent interpreter written from the template files
themselves; it shares no code with Mods Studio 2.
"""

from __future__ import annotations

import fnmatch
import functools
import math
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from . import pmd as pmdlib
from .builder import TextureJob
from .formats import TOBJ_MAGIC, tobj_bytes
from .hashfs import MAX_ENTRY_BYTES, HashFs, HashFsError, cityhash64
from .ms2data import ci_path
from .safepath import UnsafePathError, clean_member
from .xmldom import Elem

VAR_RE = re.compile(r"\{\{([^{}:]+?)(?::([A-Za-z])(\d*))?\}\}")

# xsi:type of a build action -> the Engine method that runs it
_ACTIONS = {
    "CreateDirectory": "a_create_directory",
    "CopyFile": "a_copy_file",
    "WriteTextFile": "a_write_text_file",
    "AppendTextFile": "a_append_text_file",
    "ReplaceFileText": "a_replace_file_text",
    "UpdateTextVariables": "a_update_text_variables",
    "DeleteFile": "a_delete_file",
    "DeleteDirectory": "a_delete_directory",
    "ImportImage": "a_import_image",
    "GenerateTobjFile": "a_generate_tobj_file",
    "ModifyTobjFile": "a_modify_tobj_file",
    "ModifyPmdMaterials": "a_modify_pmd_materials",
    "CleanPmdLooks": "a_clean_pmd_looks",
    "NextPmdLook": "a_next_pmd_look",
    "ConditionalAction": "a_conditional_action",
    "ProcessArrayValues": "a_process_array_values",
    "ProcessTruckInternalNamesValues": "a_process_truck_internal_names_values",
    "ProcessTruckCabinVariantsValues": "a_process_truck_cabin_variants_values",
    "ProcessAddonSlotPatternValues": "a_process_addon_slot_pattern_values",
    "ProcessTruckWindscreenTypeValues": "a_process_truck_windscreen_type_values",
    "ExtractScsArchive": "a_extract_scs_archive",
}
KNOWN_ACTIONS = set(_ACTIONS)
TOBJ_PROFILE = {"UIIcon": "ui", "TruckSkin": "skin", "TrailerSkin": "skin"}

# Limits for untrusted input: a template's regex (against catastrophic patterns), and the total
# size unpacked from another mod's archive (each member is limited by MAX_ENTRY_BYTES).
MAX_PATTERN_LEN = 1000
MAX_ARCHIVE_BYTES = 4 * 1024**3

# What a mod path stands for while the engine runs: ready bytes, a file to copy, or a texture
Entry = Union[bytes, Path, TextureJob]  # noqa: UP007 - a runtime alias, Python 3.11 compatible
# What a variable name looks up to: a variable element, or a loop value (array item, index)
Value = Union[Elem, str, int]  # noqa: UP007 - as above
Lookup = Callable[[str], "Value | None"]
# (compiled regex | None, literal | None, replacement); exactly one of the first two is set
Rule = tuple["re.Pattern[str] | None", "str | None", str]


class TemplateError(Exception):
    """A template item can't be built; the message is meant for the user."""


# ====================================================================== .NET regular expressions

# (?<name>  but not the look-behinds (?<= and (?<!
_NET_GROUP = re.compile(r"(?<!\\)\(\?<(?![=!])([A-Za-z_]\w*)>")
_NET_GROUP_Q = re.compile(r"(?<!\\)\(\?'([A-Za-z_]\w*)'")
_NET_BACKREF = re.compile(r"\\k<([A-Za-z_]\w*)>")
# $1, ${name} / ${1}, $$ and $& in a .NET replacement string
_NET_SUBST = re.compile(r"\$(?:(\d+)|\{(\w+)\}|(\$)|(&))")


def dotnet_regex(pattern: str) -> str:
    r"""Translate the .NET-only syntax of ``pattern`` to Python's ``re`` syntax.

    Named groups ``(?<n>...)`` / ``(?'n'...)`` become ``(?P<n>...)`` and ``\k<n>`` becomes
    ``(?P=n)``. Everything else is the same in both dialects for the patterns templates use.
    """
    pattern = _NET_GROUP.sub(r"(?P<\1>", pattern)
    pattern = _NET_GROUP_Q.sub(r"(?P<\1>", pattern)
    return _NET_BACKREF.sub(r"(?P=\1)", pattern)


# A group that contains a repeat and is itself repeated without an upper bound, such as (a+)+,
# (a*)* or (\w+\s?){2,}: the classic shape of a pattern that backtracks without end.
_NESTED_REPEAT = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)(?:[+*]|\{\d+,\})")


def backtracking_risk(pattern: str) -> bool:
    """Return True if ``pattern`` has a repeated group that itself contains a repeat.

    Patterns arrive inside project files that people share. Python's ``re`` has no time limit and
    holds the interpreter lock while it matches, so a pattern like ``(a+)+$`` on a few dozen
    characters would freeze the whole window. This check is a heuristic for that one well-known
    shape; it is not a proof that every other pattern is fast. Mods Studio 2's own templates use
    only simple patterns and pass.
    """
    return _NESTED_REPEAT.search(pattern) is not None


@functools.lru_cache(maxsize=256)
def compile_dotnet(pattern: str) -> re.Pattern[str]:
    """Compile a .NET regex from a template (cached).

    Raises:
        TemplateError: The pattern is too long, not valid, or could take forever to match
            (see :func:`backtracking_risk`).
    """
    if len(pattern) > MAX_PATTERN_LEN:
        raise TemplateError(f"regular expression is too long ({len(pattern)} characters)")
    if backtracking_risk(pattern):
        raise TemplateError(f"regular expression {pattern!r} is refused: a repeated group inside a repeat")
    try:
        return re.compile(dotnet_regex(pattern))
    except re.error as e:
        raise TemplateError(f"invalid regular expression {pattern!r}: {e}") from e


def expand_dotnet(rep: str, m: re.Match[str]) -> str:
    r"""The .NET replacement string ``rep`` for match ``m``.

    ``$1`` / ``${1}`` / ``${name}`` insert a group, ``$&`` (or ``$0``) the whole match and
    ``$$`` a dollar sign. A reference to a group that doesn't exist stays as written, like .NET.
    Backslashes have no special meaning (unlike Python's ``\1``).
    """
    if "$" not in rep:
        return rep

    def one(t: re.Match[str]) -> str:
        num, name, dollar, amp = t.groups()
        if dollar:
            return "$"
        if amp:
            return m.group(0)
        key: int | str = int(num) if num else (int(name) if name.isdigit() else name)
        try:
            return m.group(key) or ""
        except IndexError:
            return t.group(0)

    return _NET_SUBST.sub(one, rep)


# ====================================================================== variables


def var_list(owner: Elem) -> list[Elem]:
    """The ``<Variables>/<Variable>`` elements of a template or item."""
    v = owner.find("Variables")
    return [c for c in v.children if c.tag == "Variable"] if v is not None else []


def children(el: Elem) -> list[Elem]:
    """The ``<ChildVariables>`` of a variable (e.g. a truck's InternalName)."""
    cv = el.find("ChildVariables")
    return list(cv.children) if cv is not None else []


def selected_option(el: Elem) -> Elem | None:
    """For a list variable: the predefined option matching its value."""
    pre = el.find("PredefinedValues")
    val = el.get_text("Value")
    for o in pre.children if pre is not None else []:
        if o.get_text("Value") == val:
            return o
    return None


def child(el: Elem, name: str) -> Elem | None:
    """The child variable ``name`` of ``el``, or of its selected list option."""
    for c in children(el):
        if c.get_text("Name") == name:
            return c
    opt = selected_option(el) if el.find("PredefinedValues") is not None else None
    if opt is not None:
        for c in children(opt):
            if c.get_text("Name") == name:
                return c
    return None


def is_true(text: object) -> bool:
    """.NET's bool text: "True"/"true" (any case, surrounding space ignored)."""
    return str(text).strip().lower() == "true"


def var_text(el: Value | None) -> str:
    """The text a variable stands for in a template.

    Yes/no variables give their TrueValue/FalseValue, number variables honour their
    ``F<n>`` FormatString, a vehicle gives its internal name; others give their Value.
    """
    if el is None:
        return ""
    if isinstance(el, (str, int)):
        return str(el)
    t = el.type() or el.tag
    if t == "TwoOptionsVariable":
        return el.get_text("TrueValue") if is_true(el.get_text("Value")) else el.get_text("FalseValue")
    if t == "FloatVariable":
        raw = el.get_text("Value")
        fmt = el.get_text("FormatString").lower()
        try:
            x = float(raw)
            if fmt.startswith("f") and fmt[1:].isdigit() and math.isfinite(x):
                return f"{x:.{int(fmt[1:])}f}"
        except ValueError:
            pass  # not a number: the raw text is used, as MS2 does
        return raw
    if t == "ScsVehicleVariable":
        return el.get_text("InternalName")
    if t == "ScsImageSourceVariable" and el.find("Value") is None:
        src = el.path("ImageSource")
        if src is not None:
            return src.get_text("Value")
    return el.get_text("Value")


def array_items(el: Elem | None) -> list[str | Elem]:
    """Items of an array variable: plain strings, or variable elements (cargo, list items)."""
    if el is None:
        return []
    val = el.find("Value")
    out: list[str | Elem] = []
    for c in val.children if val is not None else []:
        if c.tag == "string":
            out.append(c.text or "")
        elif not c.children and c.text is not None and c.tag not in ("ListItemVariable",):
            out.append(c.text)
        else:
            out.append(c)
    return out


def item_text(x: str | Elem) -> str:
    """The text of an array item (see :func:`array_items`)."""
    return x if isinstance(x, str) else var_text(x)


def enabled(el: Elem, lookup: Lookup) -> bool:
    """MS2's EnabledSource: '[Var]' (a yes/no variable) or '[Var.Child]={a,b,c}'."""
    src = el.get_text("EnabledSource").strip()
    if not src:
        return True
    m = re.fullmatch(r"\[([^\]]+)\](?:=\{(.*)\})?", src)
    if not m:
        return True
    v = lookup(m.group(1))
    text = var_text(v) if v is not None else ""
    if m.group(2) is None:
        return is_true(text)
    return text in [x.strip() for x in m.group(2).split(",")]


def _texts(a: Elem, tag: str) -> list[str]:
    """The texts of the ``<string>`` children of ``a/<tag>`` (path lists of many actions)."""
    el = a.find(tag)
    return [s.text or "" for s in el.children] if el is not None else []


# ====================================================================== virtual output


@dataclass
class _PmdJob:
    """What to do to one .pmd once all actions ran (see :meth:`Engine.finish_models`)."""

    rules: list[Rule] = field(default_factory=list)  # text replacements for its materials
    matdirs: list[Path] = field(default_factory=list)  # where its .mat files are in the template
    delete_processed: bool = False  # drop the original .mat files from the mod
    clean: bool = False  # keep only the looks that NextPmdLook counted
    looks: int = 0  # looks made so far (NextPmdLook)


class Engine:
    """Build one template item into ``files`` (mod path -> bytes | Path | TextureJob).

    Mod paths are relative, '/'-separated and never contain ``..``; values are bytes (made or
    changed by an action), a Path (a file copied unchanged) or a TextureJob (an image to encode
    during the export).

    Args:
        item: The item's (or template's) XML; its ``BuildActions`` and ``Variables`` are used.
        tdir: The template folder in the MS2 installation.
        resolve_file: Finds a user file (image, mod archive) from the text of a variable.
        files: An existing mod tree to add to; a new one by default.
    """

    def __init__(
        self,
        item: Elem,
        tdir: Path,
        resolve_file: Callable[[str], Path | None],
        files: dict[str, Entry] | None = None,
    ):
        self.item = item
        self.tdir = tdir
        self.resolve_file = resolve_file
        self.files: dict[str, Entry] = files if files is not None else {}
        self.vars = {v.get_text("Name"): v for v in var_list(item)}
        self.scopes: list[dict[str, Value]] = [{}]  # loop variables, innermost last
        self.pmds: dict[str, _PmdJob] = {}
        self.warnings: list[str] = []
        # (raw source text) -> Path | None: the image to use for an empty image field
        self.fallback_image: Callable[[str], Path | None] | None = None

    # ---------------------------------------------------------------- lookup
    def lookup(self, name: str) -> Value | None:
        """Resolve a variable name: loop scopes first, then the item's variables, then ``A.B.C``."""
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        if name in self.vars:
            return self.vars[name]
        parts = name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            obj = self.lookup(head) if head != name else None
            if isinstance(obj, Elem):
                cur: Elem | None = obj
                for p in parts[i:]:
                    cur = child(cur, p) if cur is not None else None
                if cur is not None:
                    return cur
        return None

    def _subst_one(self, m: re.Match[str]) -> str:
        """The replacement of one ``{{Name[:Dn]}}``; unknown names stay as they are."""
        v = self.lookup(m.group(1).strip())
        if v is None:
            return m.group(0)
        s = var_text(v)
        if m.group(2) and m.group(2).upper() == "D":
            try:
                return str(int(s)).zfill(int(m.group(3) or 0))
            except ValueError:
                return s
        return s

    def subst(self, text: str) -> str:
        """Replace ``{{...}}`` references in ``text``; values may contain references (6 levels)."""
        if not text or "{{" not in text:
            return text or ""
        for _ in range(6):
            new = VAR_RE.sub(self._subst_one, text)
            if new == text:
                break
            text = new
        return text

    def push(self, **kv: Value) -> None:
        """Open a loop scope with the given variables."""
        self.scopes.append(dict(kv))

    def pop(self) -> None:
        """Close the innermost loop scope."""
        self.scopes.pop()

    def setvar(self, name: str, value: Value) -> None:
        """Set a variable in the innermost scope."""
        self.scopes[-1][name] = value

    # ---------------------------------------------------------------- paths
    @staticmethod
    def norm(p: str) -> str:
        """Normalise a template path to a mod path: '/' separators, no leading '/' or './'.

        A trailing '/' (meaning "a folder") is kept. ``..`` parts are resolved; a path that
        would climb above the mod root or names a drive (``C:``) is refused.

        Raises:
            TemplateError: The path leaves the mod (or the template folder).
        """
        p = p.replace("\\", "/")
        while "//" in p:
            p = p.replace("//", "/")
        p = p.lstrip("/")
        while p.startswith("./"):
            p = p[2:]
        parts = p.split("/")
        if parts and ":" in parts[0]:
            raise TemplateError(f"unsafe path in the template: {p!r}")
        if ".." not in parts:
            return p  # the usual case: unchanged
        stack: list[str] = []
        for part in parts:
            if part == "..":
                if not stack:
                    raise TemplateError(f"unsafe path in the template: {p!r} leaves its folder")
                stack.pop()
            elif part != ".":
                stack.append(part)
        trail = "/" if p.endswith("/") and stack else ""
        return "/".join(x for x in stack if x) + trail

    def out(self, raw: str) -> str:
        """The mod path of a template path text (variables substituted, normalised)."""
        return self.norm(self.subst(raw))

    def tpl(self, raw: str) -> Path | None:
        """The template-folder file of a template path text, or None if it isn't there."""
        rel = self.norm(self.subst(raw))
        return ci_path(self.tdir, rel) if rel else self.tdir

    # ---------------------------------------------------------------- file access
    def read(self, path: str) -> bytes | None:
        """The bytes of mod file ``path`` (reading a copied file into memory), or None.

        Raises:
            TemplateError: The file is a texture that is only made during the export.
        """
        e = self.files.get(path)
        if e is None:
            return None
        if isinstance(e, bytes):
            return e
        if isinstance(e, Path):
            data = e.read_bytes()
            self.files[path] = data
            return data
        raise TemplateError(f"{path} is a texture made during export and can't be edited by a template step")

    def read_text(self, path: str) -> str | None:
        """Mod file ``path`` as text (UTF-8, BOM dropped), or None."""
        b = self.read(path)
        return None if b is None else b.decode("utf-8-sig", "replace")

    def write_text(self, path: str, text: str) -> None:
        """Set mod file ``path`` to ``text`` (UTF-8, no BOM)."""
        self.files[path] = text.encode("utf-8")

    def out_glob(self, pattern: str) -> list[str]:
        """Mod paths matching ``pattern`` (wildcards in the file name only, case-insensitive)."""
        if not any(c in pattern for c in "*?["):
            return [pattern] if pattern in self.files else []
        d, _, pat = pattern.rpartition("/")
        return sorted(
            p
            for p in self.files
            if p.rpartition("/")[0].lower() == d.lower()
            and fnmatch.fnmatch(p.rpartition("/")[2].lower(), pat.lower())
        )

    # ---------------------------------------------------------------- running
    def run(self) -> dict[str, Entry]:
        """Run the item's ``BuildActions``, then rewrite the models; returns :attr:`files`."""
        self.run_actions(self.item.find("BuildActions"))
        self.finish_models()
        return self.files

    def run_actions(self, ba: Elem | None) -> None:
        """Run the ``<Action>`` children of ``ba`` in order.

        Raises:
            TemplateError: An action type is unknown, or an action fails.
        """
        for a in ba.children if ba is not None else []:
            if a.tag != "Action":
                continue
            kind = a.type()
            name = _ACTIONS.get(kind)
            if name is None:
                raise TemplateError(f"this item uses a build step Paintbooth doesn't know yet ({kind})")
            getattr(self, name)(a)

    # ---------------------------------------------------------------- simple actions
    def a_create_directory(self, a: Elem) -> None:
        """CreateDirectory (Paths): nothing to do, folders exist implicitly in a mod archive."""

    def a_delete_file(self, a: Elem) -> None:
        """DeleteFile (Paths): remove the mod files matching each path (wildcards allowed)."""
        for s in _texts(a, "Paths"):
            for p in self.out_glob(self.out(s)):
                self.files.pop(p, None)

    def a_delete_directory(self, a: Elem) -> None:
        """DeleteDirectory (Paths): remove every mod file below each folder (case-insensitive)."""
        for s in _texts(a, "Paths"):
            d = self.out(s).rstrip("/") + "/"
            for p in [p for p in self.files if p.lower().startswith(d.lower())]:
                del self.files[p]

    def _lines(self, a: Elem) -> list[str]:
        """The ``TextLines`` of an action, variables substituted."""
        return [self.subst(s) for s in _texts(a, "TextLines")]

    # like .NET's File.WriteAllLines / AppendAllLines: every line ends with CRLF
    def a_write_text_file(self, a: Elem) -> None:
        """WriteTextFile (FileName, TextLines): create or replace a text file, CRLF lines."""
        self.write_text(self.out(a.get_text("FileName")), "".join(line + "\r\n" for line in self._lines(a)))

    def a_append_text_file(self, a: Elem) -> None:
        """AppendTextFile (FileName, TextLines): add CRLF lines to a text file (created if missing)."""
        p = self.out(a.get_text("FileName"))
        old = self.read_text(p) or ""
        if old and not old.endswith("\n"):
            old += "\r\n"
        self.write_text(p, old + "".join(line + "\r\n" for line in self._lines(a)))

    def a_update_text_variables(self, a: Elem) -> None:
        """UpdateTextVariables (Paths): substitute ``{{...}}`` inside the matching mod text files."""
        for s in _texts(a, "Paths"):
            for p in self.out_glob(self.out(s)):
                t = self.read_text(p)
                if t is not None:
                    self.write_text(p, self.subst(t))

    def _replacement(self, rd: Elem) -> str:
        """The replacement text of a ReplaceData block: from a mod file, from lines, or inline."""
        if rd.find("ReplaceWithFile") is not None and rd.get_text("ReplaceWithFile"):
            return (self.read_text(self.out(rd.get_text("ReplaceWithFile"))) or "").rstrip("\r\n")
        lines = rd.find("ReplaceWithLines")
        if lines is not None and lines.children:
            return "\r\n".join(self.subst(s.text or "") for s in lines.children)
        return self.subst(rd.get_text("ReplaceWith"))

    def _rules(self, a: Elem) -> list[Rule]:
        """ReplaceData / MultiReplaceData -> [(regex, literal, replacement)].

        Raises:
            TemplateError: A SearchForRegex is not a valid regular expression.
        """
        out: list[Rule] = []
        blocks = []
        single = a.find("ReplaceData")
        if single is not None:
            blocks.append(single)
        multi = a.find("MultiReplaceData")
        if multi is not None:
            blocks += multi.children
        for rd in blocks:
            rep = self._replacement(rd)
            rx = rd.get_text("SearchForRegex")
            if rx:
                out.append((compile_dotnet(rx), None, rep))
            elif rd.get_text("SearchFor"):
                out.append((None, rd.get_text("SearchFor"), rep))
        return out

    @staticmethod
    def apply_rules(text: str, rules: list[Rule]) -> str:
        """Apply replacement rules in order: regexes with .NET replacement syntax, literals as-is."""
        for rx, lit, rep in rules:
            if rx is not None:
                text = rx.sub(lambda m, rep=rep: expand_dotnet(rep, m), text)  # type: ignore[misc]
            elif lit is not None:
                text = text.replace(lit, rep)
        return text

    def a_replace_file_text(self, a: Elem) -> None:
        """ReplaceFileText (FilePaths, ReplaceData/MultiReplaceData): search and replace in text files."""
        rules = self._rules(a)
        for s in _texts(a, "FilePaths"):
            for p in self.out_glob(self.out(s)):
                t = self.read_text(p)
                if t is not None:
                    self.write_text(p, self.apply_rules(t, rules))

    # ---------------------------------------------------------------- copying
    def _copy_tree(self, src: Path, dst: str) -> None:
        """Add every file below template folder ``src`` to the mod below ``dst``."""
        for f in sorted(src.rglob("*")):
            if f.is_file():
                self.files[self.norm(dst + "/" + f.relative_to(src).as_posix())] = f

    def _copy_dir(self, rel: str, dst: str, from_out: bool) -> bool:
        """Copy a folder: from the mod itself (a ``/`` source) or else from the template folder."""
        prefix = rel.rstrip("/") + "/" if rel else ""
        done = False
        if from_out:
            for p in [p for p in self.files if p.lower().startswith(prefix.lower())]:
                self.files[self.norm(dst + "/" + p[len(prefix) :])] = self.files[p]
                done = True
        if not done:
            src = ci_path(self.tdir, rel) if rel else self.tdir
            if src is not None and src.is_dir():
                self._copy_tree(src, dst)
                done = True
        return done

    def _copy_glob(self, rel: str, dst: str, from_out: bool) -> None:
        """Copy the files matching a wildcard name, from the mod or the template folder."""
        d, _, pat = rel.rpartition("/")
        hits: list[tuple[str, Entry]] = []
        if from_out:
            hits = [(p.rpartition("/")[2], self.files[p]) for p in self.out_glob(rel)]
        if not hits:
            src = ci_path(self.tdir, d) if d else self.tdir
            if src is not None and src.is_dir():
                hits = [
                    (f.name, f)
                    for f in sorted(src.iterdir())
                    if f.is_file() and fnmatch.fnmatch(f.name.lower(), pat.lower())
                ]
        for name, entry in hits:
            self.files[self.norm(dst + "/" + name)] = entry

    def _copy_one(self, rel: str, dst: str, from_out: bool, to_dir: bool) -> bool:
        """Copy one file: a ``/`` source prefers the mod, otherwise the template folder wins."""
        entry: Entry | None = None
        if from_out and rel in self.files:
            entry = self.files[rel]
        if entry is None:
            f = ci_path(self.tdir, rel)
            entry = f if f is not None and f.is_file() else None
        if entry is None and rel in self.files:
            entry = self.files[rel]
        if entry is None:
            return False
        name = rel.rpartition("/")[2]
        self.files[self.norm(dst + "/" + name) if to_dir else dst] = entry
        return True

    def a_copy_file(self, a: Elem) -> None:
        r"""CopyFile (Paths of From/To pairs): copy files or folders into the mod.

        ``From`` is relative to the template folder; with a leading ``\`` it may also name a
        file already in the mod. A trailing ``\`` makes it a folder, and wildcards copy the
        matching files of one folder. A ``To`` ending in ``\`` (or empty) is a folder.

        Raises:
            TemplateError: A source (other than a wildcard) was not found.
        """
        pairs = a.find("Paths")
        for pair in pairs.children if pairs is not None else []:
            raw_from = self.subst(pair.get_text("From"))
            dst = self.out(pair.get_text("To"))
            to_dir = pair.get_text("To").replace("\\", "/").endswith("/") or dst == ""
            from_out = raw_from.replace("\\", "/").startswith("/")
            rel = self.norm(raw_from)
            is_dir = raw_from.replace("\\", "/").endswith("/") or rel == ""
            if is_dir:
                done = self._copy_dir(rel, dst, from_out)
            elif any(c in rel for c in "*?["):
                self._copy_glob(rel, dst, from_out)
                done = True  # an empty wildcard match is not an error
            else:
                done = self._copy_one(rel, dst, from_out, to_dir)
            if not done:
                raise TemplateError(
                    f"Mods Studio 2 template file not found: {rel or '(template folder)'} (in {self.tdir})"
                )

    def a_extract_scs_archive(self, a: Elem) -> None:
        """ExtractScsArchive (Paths of Source/Target pairs): unpack another mod into the mod.

        Raises:
            TemplateError: The archive is missing, unreadable or too large.
        """
        pairs = a.find("Paths")
        for pair in pairs.children if pairs is not None else []:
            text = self.subst(pair.get_text("Source"))
            src = self.resolve_file(text)
            if src is None:
                raise TemplateError(f"mod file not found: {text}")
            dst = self.out(pair.get_text("Target"))
            for name, data in read_archive(src, self.warnings):
                self.files[self.norm(dst + "/" + name)] = data

    # ---------------------------------------------------------------- images & tobj
    def _image_size_rule(self, source_raw: str) -> tuple[int, int] | None:
        """The ImageSizeValidator size of the variable an image source names, if it has one."""
        m = re.fullmatch(r"\{\{([^{}:]+)\}\}", source_raw.strip())
        if not m:
            return None
        found = self.lookup(m.group(1))
        v = found if isinstance(found, Elem) else self.vars.get(m.group(1))
        vs = v.find("Validators") if v is not None else None
        for val in vs.children if vs is not None else []:
            if val.type() == "ImageSizeValidator":
                try:
                    return int(val.get_text("Width")), int(val.get_text("Height"))
                except ValueError:
                    return None
        return None

    def a_import_image(self, a: Elem) -> None:
        """ImportImage (Source, Destination, OutputFormat/Preset): a user image as a texture.

        Preset ``ScsIconImage`` makes an uncompressed sRGB shop icon (resized to the variable's
        ImageSizeValidator size); anything else a DXT5 texture with mips.

        Raises:
            TemplateError: No image was chosen and there is no fallback, or it was not found.
        """
        raw = a.get_text("Source")
        text = self.subst(raw)
        src = self.resolve_file(text) if text.strip() and "{{" not in text else None
        if src is None and self.fallback_image is not None and (not text.strip() or "{{" in text):
            src = self.fallback_image(raw)
        if src is None:
            raise TemplateError(f"image not found: {text or '(no image chosen)'}")
        dst = self.out(a.get_text("Destination"))
        fmt = a.find("OutputFormat")
        preset = fmt.get_text("Preset") if fmt is not None else ""
        if preset == "ScsIconImage":
            self.files[dst] = TextureJob(src, "icon", self._image_size_rule(raw), "BGRA_SRGB")
        else:
            self.files[dst] = TextureJob(src, "mask", None, "DXT5")

    def a_generate_tobj_file(self, a: Elem) -> None:
        """GenerateTobjFile (FileName, Content, TobjType): a .tobj pointing at the Content path."""
        content = self.subst(a.get_text("Content")).replace("\\", "/")
        if not content.startswith("/"):
            content = "/" + content
        prof = TOBJ_PROFILE.get(a.get_text("TobjType"), "skin")
        self.files[self.out(a.get_text("FileName"))] = tobj_bytes(content, profile=prof)

    def a_modify_tobj_file(self, a: Elem) -> None:
        """ModifyTobjFile (FilePaths, ReplaceData): search and replace in binary .tobj texture paths."""
        rules = self._rules(a)
        for s in _texts(a, "FilePaths"):
            for p in self.out_glob(self.out(s)):
                data = self.read(p)
                if data is None or len(data) < 48 or struct.unpack_from("<I", data, 0)[0] != TOBJ_MAGIC:
                    continue
                self.files[p] = retarget_tobj(data, lambda t: self.apply_rules(t, rules))

    # ---------------------------------------------------------------- models
    def _pmd_paths(self, a: Elem, single: str, many: str) -> list[str]:
        """The model paths of an action: one ``single`` element and/or a ``many`` list."""
        out = []
        if a.get_text(single):
            out.append(self.out(a.get_text(single)))
        out += [self.out(s) for s in _texts(a, many)]
        return [p for p in out if p]

    def a_modify_pmd_materials(self, a: Elem) -> None:
        """ModifyPmdMaterials (FilePath(s), MaterialsPath, ReplaceData, DeleteProcessedMatFiles).

        Queues the models' materials for renaming: at the end (:meth:`finish_models`) each
        material is read from MaterialsPath in the template, the replacements are applied and it
        is stored under its content hash, and the model is pointed at the new files.

        Raises:
            TemplateError: A model is not in the mod.
        """
        rules = self._rules(a)
        mat_raw = self.norm(self.subst(a.get_text("MaterialsPath")))
        matdir = ci_path(self.tdir, mat_raw) if mat_raw else self.tdir
        for p in self._pmd_paths(a, "FilePath", "FilePaths"):
            if p not in self.files:
                raise TemplateError(f"model not found in the mod: {p}")
            job = self.pmds.setdefault(p, _PmdJob())
            job.rules += rules
            if matdir is not None:
                job.matdirs.append(matdir)
            job.delete_processed = job.delete_processed or is_true(a.get_text("DeleteProcessedMatFiles"))

    def a_clean_pmd_looks(self, a: Elem) -> None:
        """CleanPmdLooks (PmdPath(s)): keep only the looks the following NextPmdLook steps count."""
        for p in self._pmd_paths(a, "PmdPath", "PmdPaths"):
            if p in self.files:
                job = self.pmds.setdefault(p, _PmdJob())
                job.clean, job.looks = True, 0

    def a_next_pmd_look(self, a: Elem) -> None:
        """NextPmdLook (PmdPath(s), LookIndexName): count one more look; its number is LookIndexName."""
        n = 0
        for p in self._pmd_paths(a, "PmdPath", "PmdPaths"):
            job = self.pmds.setdefault(p, _PmdJob())
            job.looks += 1
            n = job.looks
        name = a.get_text("LookIndexName")
        if name:
            # visible to the rest of this loop pass (e.g. the skin texture name)
            self.setvar(name, n)

    def _material(self, path: str, dirs: list[Path]) -> tuple[bytes | None, str | None]:
        """Material text (template first, then the mod) and its key in the mod if a copy is there."""
        rel = path.lstrip("/")
        in_mod = rel if rel in self.files else None
        for d in dirs:
            f = ci_path(d, rel)
            if f is not None and f.is_file():
                return f.read_bytes(), in_mod
        if in_mod:
            return self.read(rel), in_mod
        return None, None

    def _rename_materials(self, p: str, job: _PmdJob, mats: list[str]) -> dict[str, str]:
        """Rewrite each material of model ``p`` with the job's rules; old path -> new path.

        The new file is named after the CityHash64 of its content (``/automat/<hh>/<hash>.mat``),
        like MS2 and the game's own tools do, so equal materials share one file.
        """
        rename = {}
        for m in mats:
            raw, out_key = self._material(m, job.matdirs)
            if raw is None:
                raise TemplateError(f"material {m} used by {p} was not found in the template")
            new = self.apply_rules(raw.decode("utf-8", "replace"), job.rules).encode("utf-8")
            h = f"{cityhash64(new):016x}"
            newpath = f"/automat/{h[:2]}/{h}.mat"
            self.files[newpath.lstrip("/")] = new
            rename[m] = newpath
            if job.delete_processed and out_key and out_key != newpath.lstrip("/"):
                self.files.pop(out_key, None)
        return rename

    def finish_models(self) -> None:
        """Apply the queued model jobs: rename materials and fold unused looks.

        Raises:
            TemplateError: A model is damaged, or one of its materials is missing.
        """
        for p, job in self.pmds.items():
            data = self.read(p)
            if data is None:
                continue
            try:
                keep = max(1, job.looks) if job.clean else None
                table = pmdlib.material_table(data)
                mats: list[str] = []
                for row in table[:keep] if keep else table:  # only the looks that stay in use
                    mats += [m for m in row if m not in mats]
                rename = self._rename_materials(p, job, mats) if job.rules else {}
                self.files[p] = pmdlib.rewrite(data, rename, keep)
            except pmdlib.PmdError as e:
                raise TemplateError(f"{p}: {e}") from e

    # ---------------------------------------------------------------- flow control
    def _cond(self, a: Elem) -> bool:
        """Evaluate a ConditionalAction: ExecuteIf ``[Var]``, Comparison, ComparisonValue(s)."""
        expr = a.get_text("ExecuteIf").strip()
        m = re.fullmatch(r"\[([^\]]+)\]", expr)
        name = m.group(1) if m else expr
        v = self.lookup(name) if name else None
        val = var_text(v) if v is not None else ""
        vals = []
        if a.get_text("ComparisonValue"):
            vals.append(a.get_text("ComparisonValue"))
        vals += _texts(a, "ComparisonValues")
        cmp = a.get_text("Comparison", "Equals")
        if cmp == "Equals":
            return val in vals
        if cmp == "NotEquals":
            return val not in vals
        if cmp == "NotEmpty":
            return bool(val.strip())
        if cmp == "Empty":
            return not val.strip()
        if cmp == "Contains":
            return any(x in val for x in vals)
        raise TemplateError(f"unknown condition '{cmp}'")

    def a_conditional_action(self, a: Elem) -> None:
        """ConditionalAction: run BuildActions if the condition holds, else FalseBuildActions."""
        self.run_actions(a.find("BuildActions") if self._cond(a) else a.find("FalseBuildActions"))

    def _loop(self, a: Elem, scope: dict[str, Value]) -> None:
        """Run the action's BuildActions once with ``scope`` pushed."""
        self.scopes.append(scope)
        try:
            self.run_actions(a.find("BuildActions"))
        finally:
            self.pop()

    def a_process_array_values(self, a: Elem) -> None:
        """ProcessArrayValues (VariableName, ArrayItemName, ArrayIndexName, IndexOffset).

        Runs BuildActions once per array item, with the item and its index (plus IndexOffset)
        as variables (default names ITEM and INDEX).
        """
        arr = self.lookup(a.get_text("VariableName"))
        items = array_items(arr) if isinstance(arr, Elem) else []
        try:
            offset = int(a.get_text("IndexOffset", "0") or 0)
        except ValueError:
            offset = 0
        iname, xname = a.get_text("ArrayItemName") or "ITEM", a.get_text("ArrayIndexName") or "INDEX"
        for i, it in enumerate(items):
            self._loop(a, {iname: it, xname: i + offset})

    # windscreen-style loops over the selected truck's data
    def _truck_strings(self, child_name: str) -> list[str]:
        """The string array ``child_name`` of the selected Truck variable."""
        truck = self.lookup("Truck")
        c = child(truck, child_name) if isinstance(truck, Elem) else None
        return [item_text(x) for x in array_items(c)] if c is not None else []

    def a_process_truck_internal_names_values(self, a: Elem) -> None:
        """ProcessTruckInternalNamesValues: loop over Truck.InternalNames.

        Sets TRUCKINTERNALNAME (``brand.model``) and TRUCKINTERNALSHORTNAME (``model``).
        """
        for n in self._truck_strings("InternalNames") or [""]:
            short = n.split(".", 1)[1] if "." in n else n
            self._loop(a, {"TRUCKINTERNALNAME": n, "TRUCKINTERNALSHORTNAME": short})

    def a_process_truck_cabin_variants_values(self, a: Elem) -> None:
        """ProcessTruckCabinVariantsValues: loop over Truck.CabinVariants.

        Sets CABINVARIANT and CABINVARIANTSUFFIX (``_<variant>``, or "" for none).
        """
        for v in self._truck_strings("CabinVariants") or [""]:
            self._loop(a, {"CABINVARIANT": v, "CABINVARIANTSUFFIX": f"_{v}" if v else ""})

    def _choices(self, var_name: str) -> list[tuple[str, str]]:
        """Selected (value, text) of a ListItemArrayVariable; [('default', '')] when it's off or empty."""
        v = self.vars.get(var_name)
        if v is None or not enabled(v, self.lookup):
            return [("default", "")]
        sel = [
            (var_text(x) if isinstance(x, Elem) else x, x.get_text("Text") if isinstance(x, Elem) else x)
            for x in array_items(v)
        ]
        return sel or [("default", "")]

    def _suffix_name(self) -> str:
        """`` (slot text, windscreen text)`` for decal names, from the enclosing loops."""
        parts = [
            str(t)
            for t in (self.lookup("_SLOTTEXT"), self.lookup("_WSTEXT"))
            if t  # unset (None) or "" for the default choice
        ]
        return f" ({', '.join(parts)})" if parts else ""

    def a_process_addon_slot_pattern_values(self, a: Elem) -> None:
        """ProcessAddonSlotPatternValues: loop over the chosen AddonSlotPattern values.

        Sets ADDONSLOTPATTERN (``_<v>``), ADDONSLOTPATTERNWITHDOT (``.<v>``) - both "" for the
        default - and DECALNAMESUFFIX.
        """
        for val, text in self._choices("AddonSlotPattern"):
            d = val in ("", "default")
            self.push(
                ADDONSLOTPATTERN="" if d else f"_{val}",
                ADDONSLOTPATTERNWITHDOT="" if d else f".{val}",
                _SLOTTEXT="" if d else text,
            )
            self.setvar("DECALNAMESUFFIX", self._suffix_name())
            try:
                self.run_actions(a.find("BuildActions"))
            finally:
                self.pop()

    def a_process_truck_windscreen_type_values(self, a: Elem) -> None:
        """ProcessTruckWindscreenTypeValues: loop over the chosen WindscreenType values.

        Sets WINDSCREENTYPE, WINDSCREENTYPEWITHDOT, WINDSCREENDECALNAMESUFFIX and DECALNAMESUFFIX.
        """
        for val, text in self._choices("WindscreenType"):
            d = val in ("", "default")
            self.push(
                WINDSCREENTYPE="" if d else f"_{val}",
                WINDSCREENTYPEWITHDOT="" if d else f".{val}",
                _WSTEXT="" if d else text,
            )
            self.setvar("WINDSCREENDECALNAMESUFFIX", f" ({text})" if not d and text else "")
            self.setvar("DECALNAMESUFFIX", self._suffix_name())
            try:
                self.run_actions(a.find("BuildActions"))
            finally:
                self.pop()


# ====================================================================== helpers


def retarget_tobj(data: bytes, change: Callable[[str], str]) -> bytes:
    """Rewrite the texture path inside a binary .tobj (length u32 at 40, path from 48)."""
    length = struct.unpack_from("<I", data, 40)[0]
    path = data[48 : 48 + length].decode("ascii", "replace")
    new = change(path).encode("ascii", "replace")
    out = bytearray(data[:48]) + new + data[48 + length :]
    struct.pack_into("<I", out, 40, len(new))
    return bytes(out)


def _safe_member(name: str, src: Path, warnings: list[str]) -> str | None:
    """An archive member name as a safe mod path, or None (with a warning) if it is unsafe."""
    try:
        return clean_member(name, allow_root=True) or None
    except UnsafePathError:
        warnings.append(f"{src.name}: skipped {name!r}, a path outside the mod")
        return None


def _read_zip(src: Path, warnings: list[str]) -> Iterator[tuple[str, bytes]]:
    """The files of a ZIP-based mod; see :func:`read_archive`."""
    total = 0
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = _safe_member(info.filename, src, warnings)
            if name is None:
                continue
            if info.file_size > MAX_ENTRY_BYTES:
                raise TemplateError(f"{src.name}: {name} is too large to unpack")
            with z.open(info) as fh:
                data = fh.read(MAX_ENTRY_BYTES + 1)  # the size in the header can lie
            if len(data) > MAX_ENTRY_BYTES:
                raise TemplateError(f"{src.name}: {name} is too large to unpack")
            total += len(data)
            if total > MAX_ARCHIVE_BYTES:
                raise TemplateError(f"{src.name}: the archive unpacks to more than 4 GB")
            yield name, data


def _read_hashfs(src: Path, warnings: list[str]) -> Iterator[tuple[str, bytes]]:
    """The plain files of a HashFS mod; see :func:`read_archive`."""
    with HashFs(src) as fs:
        n = 0
        for p in fs.walk(""):
            name = _safe_member(p, src, warnings)
            data = fs.read(p) if name is not None else None
            if name is not None and data is not None:
                n += 1
                yield name, data
        total = sum(1 for e in fs.entries.values() if not e.is_dir)
        if fs.version == 2 and total > n:
            warnings.append(
                f"{src.name}: some textures inside this newer .scs archive can't be read by "
                f"Paintbooth; unzip-style (.zip based) mods work fully"
            )
        if n == 0:
            raise TemplateError(f"{src.name}: no readable files (the archive has no file list)")


def read_archive(src: Path, warnings: list[str]) -> Iterator[tuple[str, bytes]]:
    """Yield (path, bytes) of every file in a mod archive (.scs/.zip ZIP, or SCS HashFS).

    The archive is third-party, so member names that leave the mod (``..``, drive letters) are
    skipped with a warning, and each member's unpacked size is limited.

    Args:
        src: The archive.
        warnings: Receives messages about skipped content.

    Raises:
        TemplateError: The archive is damaged, unreadable or too large.
    """
    try:
        if zipfile.is_zipfile(src):
            yield from _read_zip(src, warnings)
        else:
            yield from _read_hashfs(src, warnings)
    except (zipfile.BadZipFile, zlib.error, HashFsError, OSError, EOFError) as e:
        raise TemplateError(f"{src.name}: can't read this mod archive ({e})") from e
