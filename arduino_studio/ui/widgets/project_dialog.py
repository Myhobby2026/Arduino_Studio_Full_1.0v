"""Project dialogs: the "New Project" form (and its small siblings).

A multi-field prompt is nicer than three chained one-line prompts, and the
structure of the created folder (``Name/Name.ino``, ``include/``, ``src/``,
``libraries/``, ``project.json``) is exactly what the user should see before
clicking *Create*, so the dialog previews the resulting tree.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import customtkinter as ctk

from ...core.project import ProjectManager
from ...core.utils import get_logger, is_valid_project_name
from ..theme import Palette
from .dialogs import StudioDialog, _hover

__all__ = ["NewProjectResult", "NewProjectDialog", "ask_new_project", "FolderPickerDialog", "ask_folder"]


@dataclass
class NewProjectResult:
    """What the user asked for in :class:`NewProjectDialog`."""

    name: str
    parent: Path
    board_fqbn: str = "arduino:avr:uno"
    description: str = ""
    author: str = ""
    with_example_files: bool = True
    open_main: bool = True

    @property
    def target(self) -> Path:
        """The folder that will be created."""
        return self.parent / self.name


class NewProjectDialog(StudioDialog):
    """Name + location + board + contents for a new sketch project."""

    def __init__(
        self,
        parent: Any,
        *,
        palette: Optional[Palette] = None,
        manager: Optional[ProjectManager] = None,
        initial_name: str = "ArduinoSketch",
        initial_parent: Optional[Path] = None,
        board_labels: Sequence[str] = (),
        selected_board: str = "",
        board_for_label: Optional[Any] = None,
    ) -> None:
        self._manager = manager or ProjectManager()
        self._board_labels = list(board_labels) or ["Arduino Uno"]
        self._selected_board = selected_board or self._board_labels[0]
        self._board_for_label = board_for_label
        self._initial_parent = Path(initial_parent) if initial_parent else self._manager.suggested_parent()
        self._initial_name = initial_name or "ArduinoSketch"
        super().__init__(parent, title="New Project", width=560, height=470, palette=palette, resizable=True)
        self._log = get_logger("ui.dialog.project")
        self._build()

    # ------------------------------------------------------------------- body
    def _build(self) -> None:
        palette = self.palette
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(14, 6))
        body.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Project name", font=(palette.font_family, 11), text_color=palette.text_dim,
                     anchor="w").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 10))
        self.name_entry = ctk.CTkEntry(body, height=30, font=(palette.font_family, 12),
                                       fg_color=palette.surface, text_color=palette.text,
                                       border_color=palette.border)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.name_entry.insert(0, self._initial_name)
        self.name_entry.bind("<KeyRelease>", lambda event: self._refresh_preview(), add=True)

        self.error_label = ctk.CTkLabel(body, text="", font=(palette.font_family, 10), text_color=palette.error,
                                        anchor="w", justify="left", wraplength=420)
        self.error_label.grid(row=1, column=1, sticky="w")

        ctk.CTkLabel(body, text="Location", font=(palette.font_family, 11), text_color=palette.text_dim,
                     anchor="w").grid(row=2, column=0, sticky="w", pady=6, padx=(0, 10))
        location = ctk.CTkFrame(body, fg_color="transparent")
        location.grid(row=2, column=1, sticky="ew", pady=6)
        location.grid_columnconfigure(0, weight=1)
        self.parent_entry = ctk.CTkEntry(location, height=30, font=(palette.mono_family, 10),
                                         fg_color=palette.surface, text_color=palette.text,
                                         border_color=palette.border)
        self.parent_entry.grid(row=0, column=0, sticky="ew")
        self.parent_entry.insert(0, str(self._initial_parent))
        ctk.CTkButton(location, text="Browse\u2026", width=84, height=30, corner_radius=6,
                      fg_color=palette.panel_alt, hover_color=palette.hover, text_color=palette.text,
                      command=self._browse).grid(row=0, column=1, sticky="e", padx=(6, 0))

        ctk.CTkLabel(body, text="Board", font=(palette.font_family, 11), text_color=palette.text_dim,
                     anchor="w").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 10))
        self.board_menu = ctk.CTkOptionMenu(body, values=self._board_labels, height=30,
                                            dropdown_font=(palette.font_family, 10), command=lambda value: self._refresh_preview())
        self.board_menu.grid(row=3, column=1, sticky="ew", pady=6)
        try:
            self.board_menu.set(self._selected_board)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

        ctk.CTkLabel(body, text="Description", font=(palette.font_family, 11), text_color=palette.text_dim,
                     anchor="w").grid(row=4, column=0, sticky="w", pady=6, padx=(0, 10))
        self.desc_entry = ctk.CTkEntry(body, height=30, font=(palette.font_family, 11),
                                       fg_color=palette.surface, text_color=palette.text,
                                       border_color=palette.border, placeholder_text="stored in project.json")
        self.desc_entry.grid(row=4, column=1, sticky="ew", pady=6)

        self.example_check = ctk.CTkCheckBox(body, text="create include/example.h and src/example.cpp",
                                             font=(palette.font_family, 10), text_color=palette.text_dim,
                                             checkbox_width=16, checkbox_height=16, command=self._refresh_preview)
        self.example_check.grid(row=5, column=1, sticky="w", pady=(8, 2))
        self.example_check.select()
        self.open_check = ctk.CTkCheckBox(body, text="open the .ino file after creating",
                                          font=(palette.font_family, 10), text_color=palette.text_dim,
                                          checkbox_width=16, checkbox_height=16)
        self.open_check.grid(row=6, column=1, sticky="w", pady=2)
        self.open_check.select()

        self.preview = ctk.CTkTextbox(body, height=112, wrap="none", font=(palette.mono_family, 10),
                                      fg_color=palette.console_bg, text_color=palette.console_fg, corner_radius=8,
                                      activate_scrollbars=False)
        self.preview.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(12, 4))
        body.grid_rowconfigure(7, weight=1)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", side="bottom", padx=20, pady=(4, 16))
        self.cancel_button = ctk.CTkButton(row, text="Cancel", width=100, height=30, corner_radius=6,
                                           fg_color="transparent", border_width=1, border_color=palette.border,
                                           hover_color=palette.hover, text_color=palette.text_dim,
                                           command=self._on_cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.create_button = ctk.CTkButton(row, text="Create", width=110, height=30, corner_radius=6,
                                           fg_color=palette.accent, hover_color=_hover(palette.accent),
                                           text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                           command=self._accept)
        self.create_button.pack(side="right")
        self._refresh_preview()

    # ------------------------------------------------------------------ logic
    def _browse(self) -> None:
        from tkinter import filedialog

        try:
            chosen = filedialog.askdirectory(parent=self, title="Where should the project live?",
                                             initialdir=str(self._parent_path()))
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen:
            return
        try:
            self.parent_entry.delete(0, "end")
            self.parent_entry.insert(0, str(Path(chosen)))
        except tk.TclError:  # pragma: no cover
            return
        self._refresh_preview()

    def _parent_path(self) -> Path:
        try:
            text = str(self.parent_entry.get()).strip()
        except (tk.TclError, AttributeError):  # pragma: no cover
            text = ""
        return Path(text).expanduser() if text else Path(self._initial_parent)

    def _project_name(self) -> str:
        try:
            return str(self.name_entry.get()).strip()
        except (tk.TclError, AttributeError):  # pragma: no cover
            return ""

    def _selected_fqbn(self) -> str:
        label = ""
        try:
            label = str(self.board_menu.get())
        except (tk.TclError, AttributeError):  # pragma: no cover
            label = ""
        if self._board_for_label is not None and label:
            try:
                return str(self._board_for_label(label)) or "arduino:avr:uno"
            except Exception:  # pragma: no cover
                return "arduino:avr:uno"
        return "arduino:avr:uno"

    def validate(self) -> Optional[str]:
        """Return an error message, or ``None`` when the form is usable."""
        name = self._project_name()
        if not name:
            return "Enter a project name."
        ok, message = is_valid_project_name(name)
        if not ok:
            return message or "That name cannot be used for a sketch folder."
        parent = self._parent_path()
        if not parent.is_dir():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return f"That folder cannot be used: {exc}"
        target = parent / name
        if target.exists():
            return f"'{target}' already exists - pick another name."
        return None

    def _refresh_preview(self) -> None:
        error = None if not self._project_name() else self.validate()
        try:
            self.error_label.configure(text=error or "")
            self.create_button.configure(state="disabled" if error else "normal")
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        name = self._project_name() or "Project"
        include_example = True
        try:
            include_example = bool(self.example_check.get())
        except (tk.TclError, AttributeError):  # pragma: no cover
            include_example = True
        lines = [f"{self._parent_path() / name}",
                 f"  {name}.ino        sketch (setup + loop)",
                 f"  project.json     board, port and file list"]
        if include_example:
            lines += ["  include/", "    example.h", "  src/", "    example.cpp"]
        lines += ["  libraries/", "    (project-local libraries)"]
        try:
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", "\n".join(lines))
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _accept(self) -> None:
        error = self.validate()
        if error:
            try:
                self.error_label.configure(text=error)
            except (tk.TclError, AttributeError):  # pragma: no cover
                pass
            return
        try:
            with_example = bool(self.example_check.get())
            open_main = bool(self.open_check.get())
            description = str(self.desc_entry.get()).strip()
        except (tk.TclError, AttributeError):  # pragma: no cover
            with_example, open_main, description = True, True, ""
        self.close(NewProjectResult(name=self._project_name(), parent=self._parent_path(),
                                    board_fqbn=self._selected_fqbn(), description=description,
                                    with_example_files=with_example, open_main=open_main))

    def _on_return(self, event: Any) -> str:
        if isinstance(event.widget, ctk.CTkTextbox):
            return ""
        self._accept()
        return "break"


class FolderPickerDialog(StudioDialog):
    """Pick an existing folder with a short label (used by *Open Project*)."""

    def __init__(self, parent: Any, *, title: str = "Choose a folder", initial: Optional[Path] = None,
                 palette: Optional[Palette] = None, hint: str = "") -> None:
        self._hint = hint
        self._initial = Path(initial) if initial else Path.home()
        super().__init__(parent, title=title, width=470, height=210, palette=palette)
        self._build()

    def _build(self) -> None:
        palette = self.palette
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(12, 6))
        body.grid_columnconfigure(0, weight=1)
        if self._hint:
            ctk.CTkLabel(body, text=self._hint, font=(palette.font_family, 10), text_color=palette.text_dim,
                         anchor="w", justify="left", wraplength=420).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.path_entry = ctk.CTkEntry(body, height=30, font=(palette.mono_family, 10), fg_color=palette.surface,
                                       text_color=palette.text, border_color=palette.border)
        self.path_entry.grid(row=1, column=0, sticky="ew")
        self.path_entry.insert(0, str(self._initial))
        ctk.CTkButton(body, text="Browse\u2026", width=90, height=30, corner_radius=6, fg_color=palette.panel_alt,
                      hover_color=palette.hover, text_color=palette.text, command=self._browse)\
            .grid(row=2, column=0, sticky="e", pady=(8, 0))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", side="bottom", padx=18, pady=(4, 14))
        ctk.CTkButton(row, text="Cancel", width=100, height=30, corner_radius=6, fg_color="transparent",
                      border_width=1, border_color=palette.border, hover_color=palette.hover,
                      text_color=palette.text_dim, command=self._on_cancel).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row, text="Open", width=96, height=30, corner_radius=6, fg_color=palette.accent,
                      hover_color=_hover(palette.accent), text_color=palette.accent_text,
                      command=self._accept).pack(side="right")

    def _browse(self) -> None:
        from tkinter import filedialog

        try:
            chosen = filedialog.askdirectory(parent=self, title="Folder", initialdir=str(self._initial))
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if chosen:
            try:
                self.path_entry.delete(0, "end")
                self.path_entry.insert(0, str(Path(chosen)))
            except tk.TclError:  # pragma: no cover
                pass

    def _accept(self) -> None:
        try:
            text = str(self.path_entry.get()).strip()
        except (tk.TclError, AttributeError):  # pragma: no cover
            text = ""
        if not text:
            return
        self.close(Path(text).expanduser())

    def _on_return(self, event: Any) -> str:
        self._accept()
        return "break"


def ask_new_project(parent: Any, *, palette: Optional[Palette] = None, manager: Optional[ProjectManager] = None,
                    initial_parent: Optional[Path] = None, board_labels: Sequence[str] = (),
                    selected_board: str = "", board_for_label: Optional[Any] = None) -> Optional[NewProjectResult]:
    """Show :class:`NewProjectDialog`; ``None`` when cancelled."""
    dialog = NewProjectDialog(parent, palette=palette, manager=manager, initial_parent=initial_parent,
                              board_labels=board_labels, selected_board=selected_board,
                              board_for_label=board_for_label)
    result = dialog.run()
    return result if isinstance(result, NewProjectResult) else None


def ask_folder(parent: Any, *, title: str = "Choose a folder", initial: Optional[Path] = None,
               palette: Optional[Palette] = None, hint: str = "") -> Optional[Path]:
    """Show :class:`FolderPickerDialog`; ``None`` when cancelled."""
    dialog = FolderPickerDialog(parent, title=title, initial=initial, palette=palette, hint=hint)
    result = dialog.run()
    return result if isinstance(result, Path) else None
