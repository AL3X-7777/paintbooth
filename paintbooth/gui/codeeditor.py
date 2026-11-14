"""Code editor for XML: line numbers, colouring, find/replace, go to line, auto-indent.

Colouring only touches the lines on screen (plus a margin), so typing stays fast in files
of any length. Gutter redraws, colouring and change notifications are all coalesced.

Keys: Ctrl+F find · Ctrl+H replace · F3 / Shift+F3 next / previous · Ctrl+G go to line ·
Tab / Shift+Tab indent / outdent · Ctrl+/ comment · Ctrl+D duplicate line ·
Ctrl+Z / Ctrl+Y undo / redo · Ctrl+plus / Ctrl+minus text size

Used by the Vehicles window (:class:`paintbooth.gui.tools.VehicleManager`) to edit vehicle XML.
Text widget indices are ``"line.column"`` strings with 1-based lines, so several helpers turn
character offsets into such indices with a bisect over the line start offsets.
"""

from __future__ import annotations

import bisect
import contextlib
import re
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from tkinter import simpledialog, ttk
from typing import ClassVar

from .theme import C

MARGIN = 40  # lines coloured above/below the visible area
MAX_MATCHES = 20000  # find stops highlighting after this many matches (keeps huge files responsive)


def _line_of(index: str) -> int:
    """Line number of a Tk text index such as ``"12.4"``."""
    return int(index.split(".", maxsplit=1)[0])


def _breaking(fn: Callable[[], object]) -> Callable[[object], str]:
    """Wrap ``fn`` as a key handler that returns "break", so Tk's default binding doesn't also run."""

    def handler(_event: object = None) -> str:
        fn()
        return "break"

    return handler


def _offset_indexer(starts: list[int], base_line: int = 1, base_col: int = 0) -> Callable[[int], str]:
    """Return a function mapping a character offset in a chunk of text to a Tk text index.

    Args:
        starts: Offsets where each line of the chunk starts (``starts[0] == 0``).
        base_line: Line number of the chunk's first line in the widget.
        base_col: Column where the chunk starts on its first line.
    """

    def idx(off: int) -> str:
        i = bisect.bisect_right(starts, off) - 1
        col = off - starts[i] + (base_col if i == 0 else 0)
        return f"{base_line + i}.{col}"

    return idx


class CodeEditor(ttk.Frame):
    """XML text editor with a line-number gutter, syntax colouring and a find/replace bar.

    Args:
        master: Parent widget.
        on_change: Called (coalesced, about 120 ms after typing stops) whenever the text changes.
    """

    # (tag name, pattern) in colouring order; "comment" is raised last so it wins over the others
    TOKENS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        ("tag", re.compile(r"</?[\w:.-]+|/?>|<\?xml|\?>")),
        ("attr", re.compile(r"\b[\w:.-]+(?==)")),
        ("string", re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')),
        ("entity", re.compile(r"&[#\w]+;")),
        ("comment", re.compile(r"<!--.*?(?:-->|\Z)", re.S)),
    ]
    INDENT = "  "

    def __init__(self, master: tk.Misc, on_change: Callable[[], object] | None = None):
        super().__init__(master)
        self.on_change = on_change
        mono = tkfont.nametofont("TkFixedFont").copy()
        for fam in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono"):
            if fam in tkfont.families():
                mono.configure(family=fam)
                break
        mono.configure(size=10)
        self.font = mono
        self._jobs: dict[str, str] = {}  # pending after() ids by job name, see _soon()
        self._find_open = False
        self._build_findbar()
        self._build_text(mono)
        self._style_tags()
        self._bind_keys()
        self._matches: list[str] = []  # start index of every find match, in text order
        self._match_len: list[int] = []

    def _build_findbar(self) -> None:
        """The find/replace bar above the text (hidden until Ctrl+F)."""
        self.findbar = fb = ttk.Frame(self, padding=(4, 4, 4, 4))
        self.find_var = tk.StringVar()
        self.repl_var = tk.StringVar()
        self.case_var = tk.BooleanVar(value=False)
        self.word_var = tk.BooleanVar(value=False)
        ttk.Label(fb, text="Find").grid(row=0, column=0, sticky="e", padx=(2, 4))
        self.find_entry = ttk.Entry(fb, textvariable=self.find_var, width=34)
        self.find_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(fb, text="Previous", command=lambda: self.find_next(-1)).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(fb, text="Next", command=lambda: self.find_next(1)).grid(row=0, column=3, padx=(2, 0))
        ttk.Checkbutton(fb, text="Match case", variable=self.case_var, command=self._find_changed).grid(
            row=0, column=4, padx=(8, 0)
        )
        ttk.Checkbutton(fb, text="Whole word", variable=self.word_var, command=self._find_changed).grid(
            row=0, column=5, padx=(6, 0)
        )
        self.count_lbl = ttk.Label(fb, text="", style="Muted.TLabel", width=16)
        self.count_lbl.grid(row=0, column=6, padx=(8, 0), sticky="w")
        ttk.Button(fb, text="Close", command=self.close_find).grid(row=0, column=7, sticky="e")
        ttk.Label(fb, text="Replace").grid(row=1, column=0, sticky="e", padx=(2, 4), pady=(4, 0))
        self.repl_entry = ttk.Entry(fb, textvariable=self.repl_var, width=34)
        self.repl_entry.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(fb, text="Replace", command=self.replace_one).grid(
            row=1, column=2, columnspan=2, padx=(4, 0), pady=(4, 0), sticky="ew"
        )
        ttk.Button(fb, text="Replace all", command=self.replace_all).grid(
            row=1, column=4, padx=(8, 0), pady=(4, 0), sticky="w"
        )
        fb.columnconfigure(1, weight=1)
        fb.columnconfigure(6, weight=1)
        self.find_var.trace_add("write", lambda *a: self._find_changed())
        self.find_entry.bind("<Return>", _breaking(lambda: self.find_next(1)))
        self.find_entry.bind("<Shift-Return>", _breaking(lambda: self.find_next(-1)))
        self.repl_entry.bind("<Return>", _breaking(self.replace_one))
        for w in (self.find_entry, self.repl_entry):
            w.bind("<Escape>", lambda e: self.close_find())

    def _build_text(self, mono: tkfont.Font) -> None:
        """The gutter canvas, the text widget and the scroll bars."""
        self.gutter = tk.Canvas(self, width=48, highlightthickness=0, bd=0)
        self.text = tk.Text(
            self,
            wrap="none",
            undo=True,
            autoseparators=True,
            maxundo=-1,
            font=mono,
            bd=0,
            highlightthickness=0,
            padx=6,
            pady=4,
            tabs=(mono.measure(self.INDENT),),
        )
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._on_yscroll, xscrollcommand=self.hbar.set)
        self.findbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.findbar.grid_remove()
        self.gutter.grid(row=1, column=0, sticky="ns")
        self.text.grid(row=1, column=1, sticky="nsew")
        self.vbar.grid(row=1, column=2, sticky="ns")
        self.hbar.grid(row=2, column=1, sticky="ew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(1, weight=1)

    def _style_tags(self) -> None:
        """Theme colours for the text and its tags, and their stacking order."""
        t = self.text
        t.configure(
            background=C["field"],
            foreground=C["fg"],
            insertbackground=C["fg"],
            selectbackground=C["select"],
            inactiveselectbackground=C["select"],
        )
        t.tag_configure("curline", background=C.get("curline", C["panel"]))
        for name, key in (
            ("tag", "code_tag"),
            ("attr", "code_attr"),
            ("string", "code_str"),
            ("entity", "code_attr"),
            ("comment", "code_comment"),
        ):
            t.tag_configure(name, foreground=C[key])
        t.tag_configure("match", background=C.get("match", "#5a4a12"))
        t.tag_configure("match_cur", background=C.get("match_cur", "#b07d10"), foreground="#000000")
        t.tag_configure("errline", background=C["errline"])
        t.tag_lower("curline")
        t.tag_raise("match")
        t.tag_raise("match_cur")
        t.tag_raise("sel")

    def _bind_keys(self) -> None:
        """Editing keys, find/zoom shortcuts and the change/scroll notifications."""
        t = self.text
        t.bind("<<Modified>>", self._modified)
        t.bind("<Configure>", lambda e: self._soon("gutter", 1, self._draw_gutter))
        t.bind("<KeyRelease>", lambda e: self._soon("cur", 10, self._mark_current_line))
        t.bind("<ButtonRelease-1>", lambda e: self._mark_current_line())
        t.bind("<Tab>", self._tab)
        t.bind("<Shift-Tab>", self._untab)
        with contextlib.suppress(tk.TclError):  # the keysym doesn't exist on Windows
            t.bind("<ISO_Left_Tab>", self._untab)  # Shift+Tab on Linux
        t.bind("<Return>", self._newline)
        t.bind("<KP_Enter>", self._newline)
        t.bind("<Control-slash>", self._toggle_comment)
        t.bind("<Control-d>", self._duplicate)
        t.bind("<Control-a>", _breaking(self._select_all))
        t.bind("<Control-y>", self._redo)
        t.bind("<Control-Shift-Z>", self._redo)
        t.bind("<Control-Shift-z>", self._redo)
        shortcuts: tuple[tuple[str, Callable[[], object]], ...] = (
            ("<Control-f>", self.open_find),
            ("<Control-h>", lambda: self.open_find(True)),
            ("<F3>", lambda: self.find_next(1)),
            ("<Shift-F3>", lambda: self.find_next(-1)),
            ("<Control-g>", self.ask_goto),
            ("<Control-plus>", lambda: self.zoom(1)),
            ("<Control-equal>", lambda: self.zoom(1)),
            ("<Control-minus>", lambda: self.zoom(-1)),
        )
        for seq, fn in shortcuts:
            t.bind(seq, _breaking(fn))
        for w in (self.find_entry, self.repl_entry):
            w.bind("<F3>", _breaking(lambda: self.find_next(1)))
            w.bind("<Shift-F3>", _breaking(lambda: self.find_next(-1)))
            w.bind("<Control-f>", _breaking(self.open_find))

    def _select_all(self) -> None:
        """Ctrl+A: select everything (Tk's default only moves the cursor to the line start)."""
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "end-1c")

    # ================================================================ scheduling
    def _soon(self, key: str, ms: int, fn: Callable[[], object]) -> None:
        """Run ``fn`` in ``ms`` milliseconds, replacing any job already pending under ``key``.

        This coalesces bursts (every keystroke, every scroll step) into one redraw or check.
        """
        old = self._jobs.pop(key, None)
        if old:
            self.after_cancel(old)

        def run() -> None:
            self._jobs.pop(key, None)
            fn()

        self._jobs[key] = self.after(ms, run)

    # ================================================================ view
    def _yview(self, *args: str) -> None:
        """Scroll bar moved: scroll the text (the gutter follows through _on_yscroll)."""
        self.text.yview(*args)

    def _on_yscroll(self, first: float, last: float) -> None:
        """Text scrolled: move the scroll bar, redraw the gutter, recolour the new lines."""
        self.vbar.set(first, last)
        self._soon("gutter", 1, self._draw_gutter)
        self._soon("hl", 40, self.highlight)

    def _draw_gutter(self) -> None:
        """Draw the line numbers of the visible lines (the current line brighter)."""
        g, t = self.gutter, self.text
        g.delete("all")
        g.configure(bg=t.cget("bg"))
        first = _line_of(t.index("@0,0"))
        last = _line_of(t.index(f"@0,{t.winfo_height()}"))
        width = max(48, self.font.measure(str(last)) + 16)
        if int(g.cget("width")) != width:
            g.configure(width=width)
        cur = _line_of(t.index("insert"))
        for n in range(first, last + 1):
            d = t.dlineinfo(f"{n}.0")
            if d is None:
                continue
            g.create_text(
                width - 6,
                d[1] + 2,
                anchor="ne",
                text=str(n),
                font=self.font,
                fill=C["fg"] if n == cur else C["muted"],
            )

    def _mark_current_line(self) -> None:
        """Highlight the line with the cursor."""
        t = self.text
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", "insert linestart", "insert lineend+1c")
        self._soon("gutter", 1, self._draw_gutter)

    def zoom(self, step: int) -> None:
        """Make the text ``step`` points larger (negative: smaller), within 7-28 points."""
        size = max(7, min(28, int(self.font.cget("size")) + step))
        self.font.configure(size=size)
        self.text.configure(tabs=(self.font.measure(self.INDENT),))
        self._soon("gutter", 1, self._draw_gutter)

    # ================================================================ change handling
    def _modified(self, _: object = None) -> None:
        """Text changed: schedule gutter, colouring, find refresh and the on_change callback."""
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)  # re-arm <<Modified>>
        self._soon("gutter", 1, self._draw_gutter)
        self._soon("hl", 60, self.highlight)
        if self._find_open and self.find_var.get():
            self._soon("find", 250, lambda: self._find_all(keep_current=True))
        if self.on_change:
            self._soon("change", 120, self.on_change)

    # ================================================================ colouring
    def highlight(self, full: bool = False) -> None:
        """Colour the visible lines plus :data:`MARGIN` lines around them (``full``: every line)."""
        t = self.text
        if full:
            first, last = 1, _line_of(t.index("end-1c"))
        else:
            first = max(1, _line_of(t.index("@0,0")) - MARGIN)
            last = _line_of(t.index(f"@0,{max(1, t.winfo_height())}")) + MARGIN
        start = f"{first}.0"
        # inside a comment that began above the region? start colouring from its "<!--"
        open_c = t.search("<!--", start, backwards=True, stopindex="1.0")
        if open_c:
            close_c = t.search("-->", open_c, stopindex=start)
            if not close_c:
                start = open_c
        end = f"{last}.end"
        chunk = t.get(start, end)
        for name, _ in self.TOKENS:
            t.tag_remove(name, start, end)
        if not chunk:
            return
        base_line, base_col = (int(x) for x in t.index(start).split("."))
        starts = [0, *(m.end() for m in re.finditer("\n", chunk))]
        idx = _offset_indexer(starts, base_line, base_col)
        for name, rx in self.TOKENS:
            ranges: list[str] = []
            for m in rx.finditer(chunk):
                if m.end() > m.start():
                    ranges += [idx(m.start()), idx(m.end())]
            if ranges:
                t.tag_add(name, *ranges)
        t.tag_raise("comment")
        t.tag_raise("match")
        t.tag_raise("match_cur")
        t.tag_raise("sel")

    # ================================================================ public API
    def get(self) -> str:
        """The whole text (without Tk's trailing newline)."""
        return self.text.get("1.0", "end-1c")

    def set(self, content: str, keep_view: bool = False) -> None:
        """Replace the text and clear the undo history.

        Args:
            content: The new text.
            keep_view: Keep the scroll position and cursor (reloading a file changed on disk).
        """
        view = self.text.yview()[0]
        cursor = self.text.index("insert")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)
        if keep_view:
            self.text.mark_set("insert", cursor)
            self.text.yview_moveto(view)
        else:
            self.text.mark_set("insert", "1.0")
            self.text.yview_moveto(0)
        self.update_idletasks()
        self.highlight()
        self._draw_gutter()
        self._mark_current_line()
        if self._find_open:
            self._find_all()

    def goto(self, line: int, mark_error: bool = True) -> None:
        """Put the cursor on ``line`` (1-based), scrolled near the top, optionally marked as an error."""
        t = self.text
        t.tag_remove("errline", "1.0", "end")
        if line > 0:
            if mark_error:
                t.tag_add("errline", f"{line}.0", f"{line}.end+1c")
            t.mark_set("insert", f"{line}.0")
            t.see(f"{line}.0")
            t.yview(f"{max(1, line - 5)}.0")
        t.focus_set()
        self._mark_current_line()

    def clear_error(self) -> None:
        """Remove the error-line highlight."""
        self.text.tag_remove("errline", "1.0", "end")

    def replace_lines(
        self, start: int, end: int, new_text: str, select: tuple[int, int] | None = None
    ) -> None:
        """Undoable replacement of lines [start, end) (1-based) with new_text."""
        t = self.text
        t.edit_separator()
        if end > start:
            t.delete(f"{start}.0", f"{end}.0")
        t.insert(f"{start}.0", new_text)
        t.edit_separator()
        if select:
            t.tag_remove("sel", "1.0", "end")
            t.tag_add("sel", f"{select[0]}.0", f"{select[1]}.0")
        t.mark_set("insert", f"{start}.0")
        t.see(f"{start + max(0, new_text.count(chr(10)) - 1)}.0")
        t.see(f"{start}.0")
        self._mark_current_line()

    # ================================================================ editing keys
    def _sel_lines(self) -> tuple[int, int] | None:
        """First and last line of the selection, or None if nothing is selected.

        A selection ending at column 0 doesn't include that last line (as in other editors).
        """
        t = self.text
        try:
            a, b = t.index("sel.first"), t.index("sel.last")
        except tk.TclError:
            return None
        la, lb = _line_of(a), _line_of(b)
        if b.endswith(".0") and lb > la:
            lb -= 1
        return la, lb

    def _current_lines(self) -> tuple[int, int]:
        """The selected lines, or the cursor's line twice."""
        sel = self._sel_lines()
        if sel is not None:
            return sel
        n = _line_of(self.text.index("insert"))
        return n, n

    def _tab(self, _: object) -> str:
        """Tab: indent the selected lines, or insert an indent at the cursor."""
        lines = self._sel_lines()
        if lines and lines[1] > lines[0]:
            t = self.text
            t.edit_separator()
            for n in range(lines[0], lines[1] + 1):
                t.insert(f"{n}.0", self.INDENT)
            t.tag_add("sel", f"{lines[0]}.0", f"{lines[1]}.end")
            t.edit_separator()
        else:
            self.text.insert("insert", self.INDENT)
        return "break"

    def _untab(self, _: object) -> str:
        """Shift+Tab: remove one indent (or one tab/space) from the selected or current lines."""
        t = self.text
        lines = self._current_lines()
        t.edit_separator()
        for n in range(lines[0], lines[1] + 1):
            line = t.get(f"{n}.0", f"{n}.end")
            if line.startswith(self.INDENT):
                t.delete(f"{n}.0", f"{n}.{len(self.INDENT)}")
            elif line.startswith(("\t", " ")):
                t.delete(f"{n}.0", f"{n}.1")
        t.edit_separator()
        return "break"

    def _newline(self, _: object) -> str:
        """Enter: keep the indent; after an opening tag indent one more (and split ``<a></a>``)."""
        t = self.text
        line = t.get("insert linestart", "insert")
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        before = line.rstrip()
        after = t.get("insert", "insert lineend").lstrip()
        opening = (
            re.search(r"<([\w:.-]+)(?:\s[^<>]*)?>$", before)
            and not before.endswith("/>")
            and not re.search(r"</[\w:.-]+>$", before)
        )
        with contextlib.suppress(tk.TclError):  # no selection
            t.delete("sel.first", "sel.last")
        if opening and after.startswith("</"):
            t.insert("insert", "\n" + indent + self.INDENT + "\n" + indent)
            t.mark_set("insert", "insert -1line lineend")
        elif opening:
            t.insert("insert", "\n" + indent + self.INDENT)
        else:
            t.insert("insert", "\n" + indent)
        t.see("insert")
        return "break"

    def _toggle_comment(self, _: object) -> str:
        """Ctrl+/: wrap the lines in ``<!-- -->``, or unwrap them if they already are."""
        t = self.text
        a, b = self._current_lines()
        block = t.get(f"{a}.0", f"{b}.end")
        stripped = block.strip()
        t.edit_separator()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            new = block.replace("<!-- ", "", 1).replace("<!--", "", 1)
            k = new.rfind(" -->")
            new = new[:k] + new[k + 4 :] if k >= 0 else new[: new.rfind("-->")] + new[new.rfind("-->") + 3 :]
        else:
            ind = block[: len(block) - len(block.lstrip(" \t"))]
            new = ind + "<!-- " + block[len(ind) :] + " -->"
        t.delete(f"{a}.0", f"{b}.end")
        t.insert(f"{a}.0", new)
        t.edit_separator()
        return "break"

    def _duplicate(self, _: object) -> str:
        """Ctrl+D: copy the selected or current lines below themselves."""
        t = self.text
        a, b = self._current_lines()
        block = t.get(f"{a}.0", f"{b}.end")
        t.edit_separator()
        t.insert(f"{b}.end", "\n" + block)
        t.edit_separator()
        return "break"

    def _redo(self, _: object) -> str:
        """Ctrl+Y / Ctrl+Shift+Z: redo (nothing to redo is not an error)."""
        with contextlib.suppress(tk.TclError):
            self.text.edit_redo()
        return "break"

    # ================================================================ find / replace
    def open_find(self, replace: bool = False) -> None:
        """Show the find bar, seeded with the selected text; ``replace`` focuses the Replace box."""
        self._find_open = True
        self.findbar.grid()
        try:
            sel = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            sel = ""
        if sel and "\n" not in sel:
            self.find_var.set(sel)
        (self.repl_entry if replace and self.find_var.get() else self.find_entry).focus_set()
        self.find_entry.select_range(0, "end")
        self._find_all()

    def close_find(self) -> None:
        """Hide the find bar and its highlights."""
        self._find_open = False
        self.findbar.grid_remove()
        self.text.tag_remove("match", "1.0", "end")
        self.text.tag_remove("match_cur", "1.0", "end")
        self.text.focus_set()

    def _find_changed(self) -> None:
        """Search text or options changed: search again shortly."""
        self._soon("find", 120, self._find_all)

    def _pattern(self) -> re.Pattern[str] | None:
        """The search as a regex (the text is literal), or None if the search box is empty."""
        q = self.find_var.get()
        if not q:
            return None
        p = re.escape(q)
        if self.word_var.get():
            p = rf"\b{p}\b"
        return re.compile(p, 0 if self.case_var.get() else re.I)

    def _find_all(self, keep_current: bool = False) -> None:
        """Highlight every match; unless ``keep_current``, jump to the first one after the cursor."""
        t = self.text
        t.tag_remove("match", "1.0", "end")
        t.tag_remove("match_cur", "1.0", "end")
        self._matches, self._match_len = [], []
        rx = self._pattern()
        if rx is None:
            self.count_lbl.configure(text="")
            return
        content = self.get()
        starts = [0, *(m.end() for m in re.finditer("\n", content))]
        idx = _offset_indexer(starts)
        ranges: list[str] = []
        for k, m in enumerate(rx.finditer(content)):
            if k >= MAX_MATCHES or m.end() == m.start():
                break
            s = idx(m.start())
            self._matches.append(s)
            self._match_len.append(m.end() - m.start())
            ranges += [s, idx(m.end())]
        if ranges:
            t.tag_add("match", *ranges)
        if not self._matches:
            self.count_lbl.configure(text="No results", style="Err.TLabel")
            return
        if not keep_current:
            self.find_next(1, from_insert=True)
        else:
            self._update_count()

    def _current_match(self) -> int:
        """Position of the current match in :attr:`_matches`, or -1."""
        t = self.text
        r = t.tag_ranges("match_cur")
        if r:
            s = str(r[0])
            if s in self._matches:
                return self._matches.index(s)
        return -1

    def _update_count(self) -> None:
        """Show "n of m" next to the find box."""
        i = self._current_match()
        n = len(self._matches)
        self.count_lbl.configure(
            text=f"{i + 1 if i >= 0 else '?'} of {n}" + ("+" if n >= MAX_MATCHES else ""),
            style="Muted.TLabel",
        )

    def find_next(self, direction: int = 1, from_insert: bool = False) -> None:
        """Select the next (``direction`` 1) or previous (-1) match, wrapping around.

        Args:
            direction: 1 for next, -1 for previous.
            from_insert: Start from the cursor instead of the current match.
        """
        if not self._find_open:
            self.open_find()
            return
        if not self._matches:
            if self.find_var.get():
                self._find_all(keep_current=True)
            if not self._matches:
                return
        t = self.text
        cur = self._current_match()
        if cur < 0 or from_insert:
            ins = t.index("insert")
            keys = [tuple(map(int, m.split("."))) for m in self._matches]
            here = tuple(map(int, ins.split(".")))
            cur = bisect.bisect_left(keys, here)
            if direction < 0:
                cur -= 1
            elif cur < len(keys) and keys[cur] == here and not from_insert:
                cur += 1
        else:
            cur += direction
        cur %= len(self._matches)
        s = self._matches[cur]
        e = f"{s}+{self._match_len[cur]}c"
        t.tag_remove("match_cur", "1.0", "end")
        t.tag_add("match_cur", s, e)
        t.tag_remove("sel", "1.0", "end")
        t.mark_set("insert", e)
        t.see(s)
        self._mark_current_line()
        self._update_count()

    def replace_one(self) -> None:
        """Replace the current match and move to the next one."""
        t = self.text
        r = t.tag_ranges("match_cur")
        if not r:
            self.find_next(1)
            return
        t.edit_separator()
        t.delete(r[0], r[1])
        t.insert(r[0], self.repl_var.get())
        t.mark_set("insert", f"{r[0]}+{len(self.repl_var.get())}c")
        t.edit_separator()
        self._find_all(keep_current=True)
        if self._matches:
            self.find_next(1, from_insert=True)

    def replace_all(self) -> None:
        """Replace every match in one undoable step."""
        rx = self._pattern()
        if rx is None:
            return
        content = self.get()
        repl = self.repl_var.get()
        new, n = rx.subn(lambda _m: repl, content)  # a function, so "\\1" in repl stays literal
        if not n:
            return
        t = self.text
        view = t.yview()[0]
        ins = t.index("insert")
        t.edit_separator()
        t.delete("1.0", "end-1c")
        t.insert("1.0", new)
        t.edit_separator()
        t.mark_set("insert", ins)
        t.yview_moveto(view)
        self._find_all(keep_current=True)
        self.count_lbl.configure(text=f"Replaced {n}", style="Muted.TLabel")

    def ask_goto(self) -> None:
        """Ask for a line number and go there."""
        last = _line_of(self.text.index("end-1c"))
        n = simpledialog.askinteger(
            "Go to line", f"Line (1-{last}):", parent=self.winfo_toplevel(), minvalue=1, maxvalue=last
        )
        if n:
            self.goto(n, mark_error=False)
