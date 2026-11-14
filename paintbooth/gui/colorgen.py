"""'Generate color' dialog: a plain 64x64 DDS swatch with any colour and opacity.

Opened from an Advanced Skin's Accessories tab (``Generate color`` and an image block's ``Color``
button). The swatch is written next to the project (``<project>/generated``) or, for an unsaved
project, into ``Documents/Paintbooth/Generated``; its name encodes colour and opacity, so making
the same swatch twice reuses one file.
"""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageDraw, ImageTk

from .. import appdirs
from ..formats import dds_bytes
from .colorpick import ColorPanel, remember_recent
from .theme import C
from .winutil import tool_window

SIZE = 64  # pixels; the game scales accessory textures, so a tiny swatch is enough

log = logging.getLogger(__name__)


def make_swatch(rgb255: tuple[int, int, int], opacity_pct: float, folder: Path) -> Path:
    """Write a 64x64 DXT5 DDS (with mips) of the colour and return its path.

    Args:
        rgb255: The colour, 0-255 sRGB.
        opacity_pct: 0-100; below 100 the paint's base colour shows through.
        folder: Where to write ``color_<rrggbb>_a<opacity>.dds`` (created if needed).

    Raises:
        OSError: The folder or file can't be written.
    """
    folder.mkdir(parents=True, exist_ok=True)
    a = round(max(0.0, min(100.0, opacity_pct)) * 255 / 100)
    hexs = "".join(f"{c:02x}" for c in rgb255)
    path = folder / f"color_{hexs}_a{round(opacity_pct):03d}.dds"
    img = Image.new("RGBA", (SIZE, SIZE), (*rgb255, a))
    path.write_bytes(dds_bytes(img, "DXT5", mips=True))
    return path


class ColorGenerator:
    """Modal dialog: pick a colour and opacity, then :meth:`run` returns the new swatch's path."""

    def __init__(self, app: Any):
        self.app = app
        self.result: Path | None = None
        self._mute = False
        w = self.win = tool_window(app, "colorgen", "Generate color - Paintbooth", resizable=False)
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        start = tuple(app.settings.get("colorgen_last", (255, 255, 255)))
        self.panel = ColorPanel(f, start, on_change=lambda rgb: self._draw(), settings=app.settings)
        self.panel.grid(row=0, column=0, columnspan=2, sticky="nsew")

        op = ttk.Frame(f)
        op.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(op, text="Opacity (%)").pack(side="left")
        self.opacity = tk.DoubleVar(value=float(app.settings.get("colorgen_opacity", 100.0)))
        self.op_text = tk.StringVar(value=f"{self.opacity.get():.0f}")
        ttk.Scale(
            op, from_=0, to=100, variable=self.opacity, length=260, command=lambda v: self._from_slider()
        ).pack(side="left", padx=(10, 0), fill="x", expand=True)
        ttk.Spinbox(
            op, from_=0, to=100, increment=1, textvariable=self.op_text, width=6, justify="right"
        ).pack(side="left", padx=(8, 0))
        self.op_text.trace_add("write", lambda *a: self._from_op_text())

        self.preview = tk.Canvas(
            f, width=64, height=64, highlightthickness=1, highlightbackground=C["border"]
        )
        self.preview.grid(row=1, column=1, rowspan=2, sticky="ne", padx=(14, 0), pady=(14, 0))
        ttk.Label(
            f,
            text="Creates a 64×64 DDS texture. Below 100% opacity the paint's base colour shows through.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bar, text="Cancel", command=w.destroy).pack(side="right")
        ttk.Button(bar, text="Create", style="Accent.TButton", command=self._ok).pack(side="right", padx=6)
        w.bind("<Return>", lambda e: self._ok())
        w.bind("<Escape>", lambda e: w.destroy())
        self._draw()

    def _from_slider(self) -> None:
        """Slider moved: update the number box."""
        if self._mute:
            return
        self._mute = True
        self.op_text.set(f"{self.opacity.get():.0f}")
        self._mute = False
        self._draw()

    def _from_op_text(self) -> None:
        """Number box edited (with either decimal separator, an optional %): move the slider."""
        if self._mute:
            return
        try:
            v = max(0.0, min(100.0, float(self.op_text.get().replace(",", ".").rstrip("%"))))
        except ValueError:
            return
        self._mute = True
        self.opacity.set(v)
        self._mute = False
        self._draw()

    def _draw(self) -> None:
        """Preview the colour at its opacity over a checkerboard."""
        if not hasattr(self, "preview"):  # ColorPanel reports its first colour before we exist
            return
        rgb = self.panel.current()
        a = round(self.opacity.get() * 255 / 100)
        bg = Image.new("RGBA", (64, 64), (70, 72, 78, 255))
        d = ImageDraw.Draw(bg)
        for y in range(0, 64, 8):
            for x in range((y // 8) % 2 * 8, 64, 16):
                d.rectangle((x, y, x + 7, y + 7), fill=(110, 112, 120, 255))
        bg.alpha_composite(Image.new("RGBA", (64, 64), (*rgb, a)))
        self._photo = ImageTk.PhotoImage(bg)
        self.preview.delete("all")
        self.preview.create_image(32, 32, image=self._photo)

    def _ok(self) -> None:
        """Create: write the swatch, remember the choice and close (an IO error keeps it open)."""
        rgb = self.panel.current()
        project_dir = self.app.project_dir()
        folder = (project_dir / "generated") if project_dir else appdirs.generated_dir()
        try:
            self.result = make_swatch(rgb, float(self.opacity.get()), folder)
        except OSError as e:
            log.warning("writing a colour swatch in %s failed: %s", folder, e)
            messagebox.showerror("Generate color", f"Could not write the texture:\n{e}", parent=self.win)
            return
        remember_recent(rgb, self.app.settings)
        self.app.settings["colorgen_last"] = list(rgb)
        self.app.settings["colorgen_opacity"] = round(float(self.opacity.get()), 1)
        self.win.destroy()

    def run(self) -> Path | None:
        """Show the dialog modally; return the swatch file, or None if cancelled."""
        self.win.wait_visibility()
        self.win.grab_set()
        self.win.wait_window()
        return self.result
