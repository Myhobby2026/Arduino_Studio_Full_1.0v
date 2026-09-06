"""Reusable themed table (``ttk.Treeview``) for list-style screens.

Used by the Library Manager (index + installed rows), the Bootloader Manager
(chips, programmers, menu options) and the first-run wizard (core picker).  It
adds what the raw widget lacks: click-to-sort headers, row tags for status
colours, a filter box, keyboard activation and a right-click menu.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Iterable, Optional, Sequence

import customtkinter as ctk

from ...core.utils import natural_key
from ..theme import Palette

__all__ = ["DataTable", "ColumnSpec"]


class ColumnSpec:
    """Definition of one table column."""

    def __init__(self, key: str, heading: str, width: int = 140, anchor: str = "w",
                 min_width: int = 60, sort: bool = True) -> None:
        self.key = key
        self.heading = heading
        self.width = width
        self.anchor = anchor
        self.min_width = min_width
        self.sort = sort

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"ColumnSpec({self.key!r}, {self.heading!r}, {self.width})"


class DataTable(ctk.CTkFrame):
    """Scrollable, sortable table with a stable key per row."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        columns: Sequence[ColumnSpec | tuple],
        *,
        height: int = 14,
        on_select: Optional[Callable[[str], Any]] = None,
        on_activate: Optional[Callable[[str], Any]] = None,
        on_right_click: Optional[Callable[[str, int, int], Any]] = None,
        row_tags: Optional[dict[str, dict[str, Any]]] = None,
        empty_text: str = "Nothing to show yet",
        selectmode: str = "extended",
        show_scrollbar: bool = True,
    ) -> None:
        super().__init__(master, fg_color=palette.surface, corner_radius=8, border_width=1,
                         border_color=palette.border)
        self.palette = palette
        self.columns: list[ColumnSpec] = [c if isinstance(c, ColumnSpec) else ColumnSpec(*c) for c in columns]
        self._on_select = on_select
        self._on_activate = on_activate
        self._on_right_click = on_right_click
        self._row_tags = dict(row_tags or {})
        self._empty_text = empty_text
        self._rows: dict[str, list[str]] = {}
        self._tags: dict[str, str] = {}
        self._sort_column: Optional[str] = None
        self._sort_reverse = False
        self._filter = ""

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        wrapper = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        wrapper.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        identifiers = [column.key for column in self.columns]
        self.tree = ttk.Treeview(
            wrapper, columns=identifiers, show="headings", style="Studio.Treeview",
            selectmode=selectmode, height=height,
        )
        for column in self.columns:
            self.tree.heading(column.key, text=column.heading,
                              command=lambda key=column.key: self.sort_by(key) if column.sort else None)
            self.tree.column(column.key, width=column.width, minwidth=column.min_width,
                             anchor=column.anchor, stretch=column.anchor == "w")
        self.scrollbar = ctk.CTkScrollbar(wrapper, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        if show_scrollbar:
            self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(2, 4))

        self.tree.bind("<<TreeviewSelect>>", lambda event: self._selected_changed(), add=True)
        self.tree.bind("<Double-1>", lambda event: self._activate_clicked(event), add=True)
        self.tree.bind("<Return>", lambda event: self._activate_clicked(event), add=True)
        self.tree.bind("<KP_Enter>", lambda event: self._activate_clicked(event), add=True)
        self.tree.bind("<Button-3>", self._right_clicked, add=True)
        self.tree.bind("<Control-a>", lambda event: self._select_all(), add=True)
        self.configure_tags(self._row_tags)

    # ------------------------------------------------------------------ theme
    def configure_tags(self, row_tags: Optional[dict[str, dict[str, Any]]] = None) -> None:
        """(Re)apply row tag colours for the current palette."""
        palette = self.palette
        defaults = {
            "normal": {"foreground": palette.text, "background": palette.surface},
            "alt": {"foreground": palette.text, "background": palette.panel_alt},
            "ok": {"foreground": palette.success},
            "warn": {"foreground": palette.warning},
            "error": {"foreground": palette.error},
            "dim": {"foreground": palette.text_muted},
            "accent": {"foreground": palette.accent},
            "installed": {"foreground": palette.success, "font": (palette.font_family, palette.font_size)},
            "available": {"foreground": palette.text},
            "update": {"foreground": palette.warning},
            "selected": {"background": palette.selected, "foreground": palette.text},
        }
        merged = dict(defaults)
        for name, options in (row_tags or {}).items():
            merged[name] = options
        self._row_tags = merged
        for name, options in merged.items():
            style: dict[str, Any] = {}
            if "foreground" in options:
                style["foreground"] = options["foreground"]
            if "background" in options:
                style["background"] = options["background"]
            if "font" in options:
                style["font"] = options["font"]
            try:
                self.tree.tag_configure(name, **style)
            except tk.TclError:  # pragma: no cover
                continue

    # -------------------------------------------------------------------- data
    def set_rows(self, rows: Iterable[tuple[str, Sequence[str]]],
                 tags: Optional[dict[str, str]] = None) -> None:
        """Replace the content.  *rows* is an iterable of ``(key, [cell, ...])``."""
        payload: dict[str, list[str]] = {}
        for key, cells in rows:
            payload[str(key)] = ["" if cell is None else str(cell) for cell in cells]
        self._rows = payload
        self._tags = {str(key): str(tag) for key, tag in (tags or {}).items()}
        self._render()

    def update_row(self, key: str, cells: Sequence[str], tag: Optional[str] = None) -> None:
        """Refresh one row in place (keeps selection and scroll position)."""
        key = str(key)
        if key not in self._rows:
            return
        self._rows[key] = ["" if cell is None else str(cell) for cell in cells]
        if tag is not None:
            self._tags[key] = tag
        try:
            self.tree.item(key, values=self._display_values(self._rows[key]),
                           tags=self._tag_names(key))
        except tk.TclError:  # pragma: no cover
            self._render()

    def add_row(self, key: str, cells: Sequence[str], tag: Optional[str] = None) -> None:
        key = str(key)
        self._rows[key] = ["" if cell is None else str(cell) for cell in cells]
        if tag:
            self._tags[key] = tag
        if self._visible(key):
            try:
                self.tree.insert("", "end", iid=key, values=self._display_values(self._rows[key]),
                                 tags=self._tag_names(key))
            except tk.TclError:  # pragma: no cover
                pass

    def remove_row(self, key: str) -> None:
        key = str(key)
        self._rows.pop(key, None)
        self._tags.pop(key, None)
        try:
            self.tree.delete(key)
        except tk.TclError:  # pragma: no cover
            pass

    def clear(self) -> None:
        """Drop every row."""
        self._rows.clear()
        self._tags.clear()
        self._render()

    def _display_values(self, cells: Sequence[str]) -> list[str]:
        return ["" if cell is None else str(cell) for cell in cells]

    def _tag_names(self, key: str) -> tuple[str, ...]:
        tag = self._tags.get(key, "normal")
        names = [tag] if tag in self._row_tags else ["normal"]
        if self._row_number(key) % 2 == 1:
            names = names + ["alt"]
        return tuple(names)

    def _row_number(self, key: str) -> int:
        try:
            return self.tree.index(key)
        except (tk.TclError, KeyError, ValueError):  # pragma: no cover
            return 0

    def _visible(self, key: str) -> bool:
        if not self._filter:
            return True
        haystack = " ".join(self._rows.get(key, [])).lower()
        return self._filter in haystack

    def _render(self) -> None:
        """Rebuild the visible rows from the model."""
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:  # pragma: no cover
            pass
        keys = list(self._rows)
        if self._sort_column in {column.key for column in self.columns}:
            index = [c.key for c in self.columns].index(self._sort_column or "")
            keys.sort(key=lambda key: natural_key(self._rows[key][index] if index < len(self._rows[key]) else ""),
                      reverse=self._sort_reverse)
        elif self._sort_column == "":
            keys.sort(key=natural_key, reverse=self._sort_reverse)
        for key in keys:
            if not self._visible(key):
                continue
            try:
                self.tree.insert("", "end", iid=key, values=self._display_values(self._rows[key]),
                                 tags=self._tag_names(key))
            except tk.TclError:  # duplicate iid (should not happen)
                continue
        if not keys:
            try:
                self.tree.insert("", "end", iid="__empty__",
                                 values=(self._empty_text,) + ("",) * (len(self.columns) - 1),
                                 tags=("dim",))
            except tk.TclError:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ sort
    def sort_by(self, column_key: str, *, reverse: Optional[bool] = None) -> None:
        """Sort rows by a column (clicking the same header flips the order)."""
        if reverse is None:
            self._sort_reverse = bool(self._sort_column == column_key and not self._sort_reverse)
        else:
            self._sort_reverse = bool(reverse)
        self._sort_column = column_key
        self._render()
        arrow = "\u25b2" if not self._sort_reverse else "\u25bc"
        for column in self.columns:
            label = f"{column.heading}  {arrow}" if column.key == self._sort_column else column.heading
            try:
                self.tree.heading(column.key, text=label)
            except tk.TclError:  # pragma: no cover
                continue

    # --------------------------------------------------------------- selection
    def selected_keys(self) -> list[str]:
        try:
            return [key for key in self.tree.selection() if key in self._rows]
        except tk.TclError:  # pragma: no cover
            return []

    def selected_key(self) -> Optional[str]:
        keys = self.selected_keys()
        return keys[0] if keys else None

    def select_key(self, key: str, *, anchor: bool = False) -> bool:
        key = str(key)
        if key not in self._rows:
            return False
        try:
            self.tree.selection_set(key)
            if anchor:
                self.tree.see(key)
        except tk.TclError:  # pragma: no cover
            return False
        return True

    def _selected_changed(self) -> None:
        if self._on_select is not None:
            key = self.selected_key()
            self._on_select(key if key is not None else "")

    def _activate_clicked(self, event: Any = None) -> str:
        """Double-click or Enter: ask the owner to open/install the row."""
        if self._on_activate is not None:
            key = self.selected_key()
            if key is not None:
                self._on_activate(key)
        return "break"

    def _right_clicked(self, event: "tk.Event") -> str:
        if self._on_right_click is None:
            return ""
        try:
            row = self.tree.identify_row(event.y)
            if row and row in self._rows:
                self.tree.selection_set(row)
        except tk.TclError:  # pragma: no cover
            row = ""
        self._on_right_click(row if row in self._rows else "", event.x_root, event.y_root)
        return "break"

    def _select_all(self) -> str:
        try:
            self.tree.selection_set(*self.tree.get_children())
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    # ------------------------------------------------------------------- misc
    def set_filter(self, text: str) -> None:
        """Restrict the visible rows to those containing *text*."""
        self._filter = (text or "").strip().lower()
        self._render()

    @property
    def filter(self) -> str:
        return self._filter

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def visible_count(self) -> int:
        try:
            return sum(1 for item in self.tree.get_children() if item in self._rows)
        except tk.TclError:  # pragma: no cover
            return 0

    def visible_keys(self) -> list[str]:
        """Row keys in display order (after sort + filter)."""
        try:
            return [item for item in self.tree.get_children() if item in self._rows]
        except tk.TclError:  # pragma: no cover
            return list(self._rows)

    def row_values(self, key: str) -> list[str]:
        return list(self._rows.get(str(key), []))

    def all_keys(self) -> list[str]:
        return list(self._rows)

    def focus_table(self) -> None:
        try:
            self.tree.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def heading_text(self, column_key: str) -> str:
        for column in self.columns:
            if column.key == column_key:
                return column.heading
        return ""

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin (theme change)."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.surface, border_color=palette.border)
        except tk.TclError:  # pragma: no cover
            pass
        self.configure_tags()
