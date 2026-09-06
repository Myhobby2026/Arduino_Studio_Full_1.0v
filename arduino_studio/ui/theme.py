"""Colour palette, fonts and Tk theming helpers.

CustomTkinter handles its own widgets, but the code editor, console and file
tree are plain ``tkinter`` widgets (they need tags, marks and per-character
colouring).  This module keeps both families visually consistent.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["Palette", "SYNTAX_LIGHT", "SYNTAX_DARK", "palette_for", "apply_tk_theme", "EDITOR_TAGS"]


@dataclass(frozen=True)
class Palette:
    """Colours + fonts for one appearance mode."""

    name: str
    window_bg: str
    panel_bg: str
    panel_alt: str
    surface: str
    border: str
    text: str
    text_dim: str
    text_muted: str
    accent: str
    accent_text: str
    hover: str
    selected: str
    success: str
    warning: str
    error: str
    info: str
    # editor
    editor_bg: str
    editor_fg: str
    editor_gutter_bg: str
    editor_gutter_fg: str
    editor_gutter_active: str
    editor_current_line: str
    editor_selection: str
    editor_bracket: str
    editor_find: str
    editor_find_current: str
    editor_line_active_fg: str
    # console
    console_bg: str
    console_fg: str
    console_dim: str
    console_selection: str
    font_family: str = "Segoe UI"
    font_size: int = 10
    mono_family: str = "Cascadia Code"
    mono_size: int = 12
    accent_hover: str = "#4a94ff"


DARK = Palette(
    name="Dark",
    window_bg="#18181e",
    panel_bg="#1f1f27",
    panel_alt="#262630",
    surface="#2b2b36",
    border="#3a3a47",
    text="#e6e6ea",
    text_dim="#b6b6c2",
    text_muted="#8a8a99",
    accent="#2f81f7",
    accent_text="#ffffff",
    hover="#33333f",
    selected="#2c4a7c",
    success="#3fb950",
    warning="#d29922",
    error="#f85149",
    info="#58a6ff",
    editor_bg="#15151b",
    editor_fg="#dcdce4",
    editor_gutter_bg="#121217",
    editor_gutter_fg="#5f6370",
    editor_gutter_active="#c9c9d4",
    editor_current_line="#1e1e27",
    editor_selection="#2c4a7c",
    editor_bracket="#3f6f3f",
    editor_find="#5a4a1f",
    editor_find_current="#7a5f16",
    editor_line_active_fg="#d6d6e0",
    console_bg="#101014",
    console_fg="#cfe3ff",
    console_dim="#7c8698",
    console_selection="#2c4a7c",
)

LIGHT = Palette(
    name="Light",
    window_bg="#f3f3f6",
    panel_bg="#eaeaef",
    panel_alt="#e0e0e7",
    surface="#ffffff",
    border="#c8c8d2",
    text="#1c1c22",
    text_dim="#41414d",
    text_muted="#6a6a78",
    accent="#1f6feb",
    accent_text="#ffffff",
    hover="#dcdce4",
    selected="#c4dcff",
    success="#1a7f37",
    warning="#9a6700",
    error="#cf222e",
    info="#0969da",
    editor_bg="#ffffff",
    editor_fg="#1f2328",
    editor_gutter_bg="#f4f4f7",
    editor_gutter_fg="#8a8a99",
    editor_gutter_active="#24292f",
    editor_current_line="#f2f6fc",
    editor_selection="#c4dcff",
    editor_bracket="#c8f0c8",
    editor_find="#fff3b0",
    editor_find_current="#ffd866",
    editor_line_active_fg="#0a3069",
    console_bg="#fbfbfd",
    console_fg="#24292f",
    console_dim="#6a737d",
    console_selection="#c4dcff",
)


#: Syntax colours for the editor (Arduino / C++).
SYNTAX_DARK: dict[str, str] = {
    "keyword": "#ff7b72",
    "type": "#79c0ff",
    "constant": "#79c0ff",
    "arduino_api": "#d2a8ff",
    "pin_constant": "#ffa657",
    "string": "#a5d6ff",
    "char": "#a5d6ff",
    "number": "#79c0ff",
    "comment": "#8b949e",
    "preprocessor": "#f2cc60",
    "function": "#d2a8ff",
    "macro": "#f2cc60",
    "operator": "#ff7b72",
    "bracket": "#c9d1d9",
    "define": "#f2cc60",
    "error": "#f85149",
    "todo": "#f2cc60",
    "boolean": "#79c0ff",
    "scope": "#c9d1d9",
}

SYNTAX_LIGHT: dict[str, str] = {
    "keyword": "#cf222e",
    "type": "#0550ae",
    "constant": "#0550ae",
    "arduino_api": "#8250df",
    "pin_constant": "#953800",
    "string": "#0a3069",
    "char": "#0a3069",
    "number": "#0550ae",
    "comment": "#6e7781",
    "preprocessor": "#9a6700",
    "function": "#8250df",
    "macro": "#9a6700",
    "operator": "#cf222e",
    "bracket": "#24292f",
    "define": "#9a6700",
    "error": "#cf222e",
    "todo": "#9a6700",
    "boolean": "#0550ae",
    "scope": "#24292f",
}

#: Tags the editor installs (kept here so tests can assert on them).
EDITOR_TAGS: tuple[str, ...] = (
    "keyword", "type", "constant", "boolean", "arduino_api", "pin_constant", "string", "char",
    "number", "comment", "preprocessor", "function", "macro", "operator", "bracket", "define",
    "todo", "scope", "current_line", "bracket_match", "bracket_error", "found", "found_current",
    "error_line", "error_marker", "warn_line", "diff_added", "selection_style",
)


def palette_for(appearance: str = "Dark", accent: Optional[str] = None) -> Palette:
    """Return the :class:`Palette` for ``"Dark"`` / ``"Light"`` / ``"System"``."""
    base = LIGHT if str(appearance).lower().startswith("light") else DARK
    if accent and accent != base.accent:
        base = _replace_accent(base, accent)
    return base


def _replace_accent(base: Palette, accent: str) -> Palette:
    from dataclasses import replace

    return replace(base, accent=accent, accent_hover=lighten(accent, 0.14))


def apply_tk_theme(root: tk.Misc, palette: Palette) -> None:
    """Theme the plain Tk widgets (menus, text, treeview, scrollbars)."""
    tk_root = root if isinstance(root, tk.Tk) else root.winfo_toplevel()
    try:
        option = tk_root.option_add
        option("*TearOff", False)
        option("*Font", (palette.font_family, palette.font_size))
        option("*Menu*Font", (palette.font_family, palette.font_size))
        option("*Menu*background", palette.panel_alt)
        option("*Menu*foreground", palette.text)
        option("*Menu*activeBackground", palette.accent)
        option("*Menu*activeForeground", palette.accent_text)
        option("*Menu*borderWidth", 0)
        option("*Menu*relief", "flat")
        option("*Menu*padding", 6)
        option("*Listbox*background", palette.panel_bg)
        option("*Listbox*foreground", palette.text)
        option("*Listbox*selectBackground", palette.accent)
        option("*Listbox*selectForeground", palette.accent_text)
        option("*Listbox*borderWidth", 0)
        option("*Text*insertBackground", palette.text)
        option("*Text*highlightBackground", palette.panel_bg)
        option("*Text*selectBackground", palette.editor_selection)
        option("*Text*selectForeground", palette.editor_fg)
        option("*Scrollbar*background", palette.panel_alt)
        option("*Scrollbar*troughColor", palette.panel_bg)
        option("*Scrollbar*borderWidth", 0)
        option("*Scrollbar*relief", "flat")
        option("*Frame*background", palette.panel_bg)
        option("*Canvas*background", palette.panel_bg)
        option("*Scale*background", palette.panel_bg)
        option("*Scale*troughColor", palette.panel_alt)
        option("*Checkbutton*background", palette.panel_bg)
        option("*Checkbutton*foreground", palette.text)
        option("*Radiobutton*background", palette.panel_bg)
        option("*Radiobutton*foreground", palette.text)
        option("*Label*background", palette.panel_bg)
        option("*Entry*background", palette.surface)
        option("*Entry*foreground", palette.text)
        option("*Entry*insertBackground", palette.text)
        option("*Treeview*background", palette.panel_bg)
        option("*Treeview*fieldBackground", palette.panel_bg)
        option("*Treeview*foreground", palette.text)
    except tk.TclError:  # pragma: no cover - option DB may be unavailable
        pass

    style: Optional[Any] = None
    try:
        from tkinter import ttk

        style = ttk.Style(tk_root)
    except (ImportError, tk.TclError):  # pragma: no cover
        style = None
    if style is None:
        return
    try:
        style.theme_use("clam" if "clam" in style.theme_names() else style.theme_use())
    except tk.TclError:  # pragma: no cover
        return
    style.configure(
        "Studio.Treeview",
        background=palette.panel_bg,
        fieldbackground=palette.panel_bg,
        foreground=palette.text,
        rowheight=22,
        borderwidth=0,
        relief="flat",
        font=(palette.mono_family, max(9, palette.font_size)),
    )
    style.configure("Studio.Treeview.Item", padding=(3, 2))
    style.configure(
        "Studio.Treeview.Heading",
        background=palette.panel_alt,
        foreground=palette.text_dim,
        borderwidth=0,
        relief="flat",
        font=(palette.font_family, palette.font_size),
    )
    style.map(
        "Studio.Treeview",
        background=[("selected", palette.selected)],
        foreground=[("selected", palette.text)],
        expandcell=[("selected", palette.selected)],
        indicatorcolor=[("selected", palette.accent)],
    )
    style.map(
        "Studio.Treeview.Heading",
        background=[("active", palette.hover)],
    )
    style.layout("Studio.Treeview", [
        ("Treeview.treearea", {"sticky": "nswe"}),
    ])
    style.configure(
        "Studio.Vertical.TScrollbar",
        background=palette.panel_alt,
        troughcolor=palette.panel_bg,
        bordercolor=palette.panel_bg,
        arrowcolor=palette.text_dim,
        arrowsize=12,
        relief="flat",
    )
    style.configure(
        "Studio.Horizontal.TScrollbar",
        background=palette.panel_alt,
        troughcolor=palette.panel_bg,
        bordercolor=palette.panel_bg,
        arrowcolor=palette.text_dim,
        relief="flat",
    )
    style.map(
        "Studio.Vertical.TScrollbar",
        background=[("active", palette.hover), ("pressed", palette.accent)],
    )
    style.configure(
        "Studio.Horizontal.TProgressbar",
        background=palette.accent,
        troughcolor=palette.panel_alt,
        borderwidth=0,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
    )
    style.configure("Studio.Horizontal.TPanedWindow", background=palette.border, sashwidth=6, borderwidth=0)
    style.configure(
        "Studio.TNotebook",
        background=palette.panel_bg,
        tabmargins=(4, 2, 4, 0),
        borderwidth=0,
    )
    style.configure(
        "Studio.TNotebook.Tab",
        background=palette.panel_alt,
        foreground=palette.text_dim,
        padding=(12, 5),
        borderwidth=0,
        font=(palette.font_family, palette.font_size),
    )
    style.map(
        "Studio.TNotebook.Tab",
        background=[("selected", palette.surface), ("active", palette.hover)],
        foreground=[("selected", palette.text)],
    )


def blend(color_a: str, color_b: str, ratio: float = 0.5) -> str:
    """Mix two ``#rrggbb`` colours (used for hover tints)."""
    def unpack(color: str) -> tuple[int, int, int]:
        text = (color or "#000000").lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        except ValueError:
            return 0, 0, 0

    r1, g1, b1 = unpack(color_a)
    r2, g2, b2 = unpack(color_b)
    ratio = max(0.0, min(1.0, ratio))
    mixed = tuple(int(a * (1 - ratio) + b * ratio) for a, b in ((r1, r2), (g1, g2), (b1, b2)))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def darken(color: str, amount: float = 0.2) -> str:
    return blend(color, "#000000", amount)


def lighten(color: str, amount: float = 0.2) -> str:
    return blend(color, "#ffffff", amount)
