"""Status bar (bottom strip): project, board, port, operation, clock."""

from __future__ import annotations

import tkinter as tk
from typing import Any, Optional

import customtkinter as ctk

from ..theme import Palette

__all__ = ["StatusBar"]


class StatusBar(ctk.CTkFrame):
    """Thin informative strip - never interactive except the status label."""

    def __init__(self, master: Any, palette: Palette, *, on_status_click: Optional[Any] = None) -> None:
        super().__init__(master, fg_color=palette.panel_alt, corner_radius=0, height=26)
        self.palette = palette
        self.grid_propagate(False)
        self.grid_rowconfigure(0, weight=1)
        self._timer_job: Optional[str] = None
        self._started: Optional[float] = None

        self.grid_columnconfigure(6, weight=1)
        self.project_label = self._cell(0, "no project", width=200, anchor="w")
        self._divider(1)
        self.board_label = self._cell(2, "no board", width=210, anchor="w")
        self._divider(3)
        self.port_label = self._cell(4, "no port", width=140, anchor="w")
        self._divider(5)
        self.message_label = self._cell(6, "ready", width=10, anchor="w")
        self.elapsed_label = self._cell(7, "", width=76, anchor="e")
        self.mem_label = self._cell(8, "", width=210, anchor="e")
        self.version_label = self._cell(9, "Arduino Studio 1.0", width=140, anchor="e")
        for label in (self.project_label, self.board_label, self.port_label, self.message_label):
            label.bind("<Button-3>", self._copy_status, add=True)

    # ------------------------------------------------------------------ build
    def _cell(self, column: int, text: str, *, width: int, anchor: str) -> ctk.CTkLabel:
        label = ctk.CTkLabel(
            self, text=text, width=width, anchor=anchor,
            font=(self.palette.font_family, 10), text_color=self.palette.text_muted,
        )
        label.grid(row=0, column=column, sticky="ns", padx=(10 if anchor == "w" else 0, 0), pady=2)
        return label

    def _divider(self, column: int) -> None:
        line = ctk.CTkFrame(self, width=1, height=14, fg_color=self.palette.border, corner_radius=0)
        line.grid(row=0, column=column, sticky="ns", pady=6)

    def _copy_status(self, event: "tk.Event") -> str:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.message_label.cget("text"))
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        return "break"

    # ------------------------------------------------------------------- text
    def set_project(self, name: str, *, modified: bool = False) -> None:
        """Project name (with a dot when it has unsaved changes)."""
        text = name or "no project"
        if modified and name:
            text = f"{text}  \u25cf"
        self._set(self.project_label, text)

    def set_board(self, text: str) -> None:
        self._set(self.board_label, text or "no board")

    def set_port(self, text: str, *, connected: bool = False) -> None:
        label = text or "no port"
        try:
            self.port_label.configure(text_color=self.palette.success if connected else self.palette.text_muted)
        except tk.TclError:  # pragma: no cover
            pass
        self._set(self.port_label, label)

    def set_message(self, text: str) -> None:
        """Left-aligned status message (also used for errors)."""
        self._set(self.message_label, text)

    def set_error(self, text: str) -> None:
        try:
            self.message_label.configure(text=text, text_color=self.palette.error)
        except tk.TclError:  # pragma: no cover
            pass

    def _set(self, label: ctk.CTkLabel, text: str) -> None:
        try:
            label.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def set_board_color(self, colour: str) -> None:
        try:
            self.board_label.configure(text_color=colour)
        except tk.TclError:  # pragma: no cover
            pass

    # --------------------------------------------------------------- operation
    def start_operation(self, label: str) -> None:
        """Show 'label\u2026' with a running elapsed timer."""
        self._started = None
        import time

        self._started = time.perf_counter()
        self._set(self.message_label, label)
        if self._timer_job is None:
            self._tick()

    def _tick(self) -> None:
        import time

        if self._started is None:
            self._timer_job = None
            return
        elapsed = time.perf_counter() - self._started
        try:
            self.elapsed_label.configure(text=f"{elapsed:.1f} s")
        except tk.TclError:  # pragma: no cover
            self._timer_job = None
            return
        self._timer_job = self.after(500, self._tick)

    def finish_operation(self, ok: bool, summary: str = "") -> None:
        """Stop the timer and show the result."""
        self._started = None
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._timer_job = None
        try:
            self.elapsed_label.configure(text="")
            self.message_label.configure(text=summary or ("done" if ok else "failed"),
                                         text_color=self.palette.success if ok else self.palette.error)
        except tk.TclError:  # pragma: no cover
            pass

    def set_memory(self, text: str) -> None:
        """Flash/RAM usage from ``arduino-cli compile``."""
        self._set(self.mem_label, text)

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.panel_alt)
            for label in (self.project_label, self.board_label, self.port_label, self.message_label,
                          self.elapsed_label, self.mem_label, self.version_label):
                label.configure(text_color=palette.text_muted)
        except tk.TclError:  # pragma: no cover
            pass

    def destroy(self) -> None:
        """Cancel the tick timer before the widget goes away."""
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._timer_job = None
        super().destroy()
