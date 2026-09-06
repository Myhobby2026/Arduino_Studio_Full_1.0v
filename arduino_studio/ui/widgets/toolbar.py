"""Top toolbar: project actions, build/upload, board & port selectors.

The toolbar is a :class:`customtkinter.CTkFrame` with icon buttons. Board and
port are :class:`customtkinter.CTkOptionMenu` widgets fed by the Arduino CLI;
while a query runs the widgets are disabled so the user cannot pick a half
populated list.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Iterable, Optional

import customtkinter as ctk

from ..icons import icon_text
from ..theme import Palette  # noqa: F401  (re-exported for type checkers)

__all__ = ["Toolbar"]


class Toolbar(ctk.CTkFrame):
    """One-row command bar under the window title."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        on_new_project: Optional[Callable[[], Any]] = None,
        on_open_project: Optional[Callable[[], Any]] = None,
        on_save: Optional[Callable[[], Any]] = None,
        on_verify: Optional[Callable[[], Any]] = None,
        on_upload: Optional[Callable[[], Any]] = None,
        on_board_selected: Optional[Callable[[str], Any]] = None,
        on_port_selected: Optional[Callable[[str], Any]] = None,
        on_serial: Optional[Callable[[], Any]] = None,
        on_libraries: Optional[Callable[[], Any]] = None,
        on_bootloader: Optional[Callable[[], Any]] = None,
        on_settings: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.panel_bg, corner_radius=0, height=46,
                         border_width=0)
        self.palette = palette
        self.grid_propagate(False)
        self.grid_rowconfigure(0, weight=1)
        self._handlers: dict[str, Callable[[], Any]] = {
            "new_project": on_new_project or (lambda: None),
            "open_project": on_open_project or (lambda: None),
            "save": on_save or (lambda: None),
            "verify": on_verify or (lambda: None),
            "upload": on_upload or (lambda: None),
            "serial": on_serial or (lambda: None),
            "libraries": on_libraries or (lambda: None),
            "bootloader": on_bootloader or (lambda: None),
            "settings": on_settings or (lambda: None),
        }
        self._on_board_selected = on_board_selected
        self._on_port_selected = on_port_selected
        self.grid_columnconfigure(99, weight=1)

        column = 0
        self.new_button = self._button(column, "new_project", "new_project", "New project  (Ctrl+Shift+N)")
        column += 1
        self.open_button = self._button(column, "open_project", "open", "Open project  (Ctrl+O)")
        column += 1
        self.save_button = self._button(column, "save", "save", "Save  (Ctrl+S)")
        column += 1
        self._separator(column)
        column += 1
        self.verify_button = self._accent_button(column, "verify", "verify",
                                                 "Verify / compile  (Ctrl+Shift+R)")
        column += 1
        self.upload_button = self._accent_button(column, "upload", "upload", "Upload to board  (Ctrl+Shift+U)")
        column += 1
        self._separator(column)
        column += 1

        self.board_menu = self._selector(
            column, "Board", ("arduino:avr:uno",), self._board_changed,
            "Board / FQBN - choose the target, or type a custom FQBN in Board settings",
        )
        column += 1
        self.port_menu = self._selector(
            column, "Port", ("Select a port",), self._port_changed,
            "Serial port of the connected board",
        )
        column += 1
        self.refresh_button = self._button(column, "refresh", "refresh", "Refresh board list and ports")
        self._handlers["refresh"] = lambda: None
        column += 1
        self._separator(column)
        column += 1

        self.serial_button = self._button(column, "serial", "serial", "Open the Serial Monitor  (Ctrl+Shift+M)")
        column += 1
        self.libraries_button = self._button(column, "libraries", "libraries",
                                             "Manage Arduino libraries  (Ctrl+Shift+L)")
        column += 1
        self.bootloader_button = self._button(column, "bootloader", "bootloader",
                                              "Burn bootloaders / flash binaries")
        column += 1
        self.settings_button = self._button(column, "settings", "settings", "Preferences  (Ctrl+,)")

    # ---------------------------------------------------------------- widgets
    def _button(self, column: int, action: str, icon: str, tooltip: str) -> ctk.CTkButton:
        palette = self.palette
        button = ctk.CTkButton(
            self,
            text=icon_text(icon),
            width=0,
            height=30,
            corner_radius=8,
            padx=12,
            fg_color="transparent",
            hover_color=palette.hover,
            text_color=palette.text,
            font=(palette.font_family, 11),
            command=lambda: self._invoke(action),
        )
        button.grid(row=0, column=column, sticky="w", padx=(4, 0), pady=6)
        setattr(self, f"{action}_button", button)
        return button

    def _accent_button(self, column: int, action: str, icon: str, tooltip: str) -> ctk.CTkButton:
        palette = self.palette
        button = ctk.CTkButton(
            self,
            text=icon_text(icon),
            width=0,
            height=30,
            corner_radius=8,
            padx=14,
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            text_color=palette.accent_text,
            font=(palette.font_family, 11, "bold"),
            command=lambda: self._invoke(action),
        )
        button.grid(row=0, column=column, sticky="w", padx=(4, 0), pady=6)
        return button

    def _selector(self, column: int, label: str, values: Iterable[str],
                  command: Callable[[str], Any], tooltip: str) -> ctk.CTkOptionMenu:
        palette = self.palette
        menu = ctk.CTkOptionMenu(
            self,
            values=list(values),
            width=250,
            height=30,
            corner_radius=8,
            dropdown_font=(palette.font_family, 10),
            font=(palette.font_family, 11),
            anchor="w",
            command=command,
        )
        menu.grid(row=0, column=column, sticky="w", padx=(6, 0), pady=6)
        return menu

    def _separator(self, column: int) -> None:
        line = ctk.CTkFrame(self, width=1, height=26, fg_color=self.palette.border, corner_radius=0)
        line.grid(row=0, column=column, sticky="ns", padx=(8, 2), pady=10)

    def set_refresh_handler(self, handler: Callable[[], Any]) -> None:
        """Wire the refresh button once the app owns the CLI service."""
        self._handlers["refresh"] = handler

    # ------------------------------------------------------------------ hooks
    def _invoke(self, action: str) -> None:
        handler = self._handlers.get(action)
        if handler is not None:
            handler()

    def set_handler(self, action: str, handler: Callable[[], Any]) -> None:
        """Install or replace one toolbar callback (used by :mod:`app`)."""
        self._handlers[action] = handler

    def _board_changed(self, value: str) -> None:
        if self._on_board_selected is not None:
            self._on_board_selected(value)

    def _port_changed(self, value: str) -> None:
        if self._on_port_selected is not None:
            self._on_port_selected(value)

    # ------------------------------------------------------------------- data
    def set_boards(self, labels: list[str], selected: Optional[str] = None) -> None:
        """Replace the board dropdown entries (labels; app maps to FQBNs)."""
        try:
            self.board_menu.configure(values=labels or ["No boards installed"])
            if selected:
                self.board_menu.set(selected)
        except tk.TclError:  # pragma: no cover
            pass

    def set_ports(self, labels: list[str], selected: Optional[str] = None) -> None:
        """Replace the port dropdown entries."""
        try:
            self.port_menu.configure(values=labels or ["No ports found"])
            if selected:
                self.port_menu.set(selected)
        except tk.TclError:  # pragma: no cover
            pass

    def current_board(self) -> str:
        try:
            return str(self.board_menu.get())
        except tk.TclError:  # pragma: no cover
            return ""

    def current_port(self) -> str:
        try:
            return str(self.port_menu.get())
        except tk.TclError:  # pragma: no cover
            return ""

    def set_busy(self, busy: bool, *, disable_upload: bool = True) -> None:
        """Grey out the action buttons while a task runs."""
        state = "disabled" if busy else "normal"
        for name in ("verify_button", "upload_button", "new_button", "open_button", "refresh_button"):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            if name == "upload_button" and not disable_upload:
                continue
            try:
                widget.configure(state=state)
            except tk.TclError:  # pragma: no cover
                pass

    def set_upload_enabled(self, enabled: bool) -> None:
        try:
            self.upload_button.configure(state="normal" if enabled else "disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def set_save_enabled(self, enabled: bool) -> None:
        try:
            self.save_button.configure(state="normal" if enabled else "disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def set_board_enabled(self, enabled: bool) -> None:
        for widget in (self.board_menu, self.port_menu):
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:  # pragma: no cover
                pass

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin the toolbar after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.panel_bg)
        except tk.TclError:  # pragma: no cover
            pass

