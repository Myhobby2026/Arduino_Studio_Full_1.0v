"""Tabbed editor container: one :class:`CodeEditor` per open file.

The tabs themselves are CustomTkinter frames inside a canvas so the strip can
scroll when many files are open; editors live in a stacked frame and are shown
one at a time (never destroyed) so undo history and scroll position survive a
tab switch.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import customtkinter as ctk

from ..core.project import Project, ProjectError
from ..core.utils import get_logger
from .code_editor import CodeEditor, EditorDocument
from .theme import Palette

__all__ = ["EditorTabs"]


class TabButton(ctk.CTkFrame):
    """A single tab: name, unsaved dot, close button."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        document: EditorDocument,
        *,
        on_select: Callable[[], Any],
        on_close: Callable[[], Any],
        on_middle_click: Optional[Callable[[], Any]] = None,
        on_right_click: Optional[Callable[[int, int], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=8, height=30)
        self.palette = palette
        self.document = document
        self.selected = False
        self._dirty_shown = False
        self._on_select = on_select
        self._on_close = on_close
        self._on_middle_click = on_middle_click
        self._on_right_click = on_right_click

        self.body = ctk.CTkFrame(self, fg_color="transparent", corner_radius=8)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.close_button = ctk.CTkButton(
            self.body, text="\u2715", width=22, height=22, corner_radius=4,
            fg_color="transparent", hover_color=palette.hover, text_color=palette.text_muted,
            font=(palette.font_family, 10), command=lambda: self._on_close(),
        )
        self.close_button.pack(side="right", padx=(2, 6))
        self.dirty_label = ctk.CTkLabel(self.body, text="\u25cf", width=0,
                                        font=(palette.font_family, 11), text_color=palette.warning)
        self.title = ctk.CTkLabel(self.body, text=document.name, font=(palette.font_family, 11),
                                  text_color=palette.text_dim, anchor="w")
        self.title.pack(side="left", padx=(10, 6))
        self._apply_style()

        for widget in (self, self.body, self.title):
            widget.bind("<Button-1>", lambda event: self._on_select(), add=True)
            widget.bind("<Button-2>", lambda event: self._middle_click(), add=True)
            widget.bind("<Button-3>", lambda event: self._right_click(event), add=True)
            widget.bind("<MouseWheel>", lambda event: self._wheel(event), add=True)
            widget.bind("<Enter>", lambda event: self._hover(True), add=True)
            widget.bind("<Leave>", lambda event: self._hover(False), add=True)

    # ------------------------------------------------------------------ events
    def _middle_click(self) -> None:
        if self._on_middle_click is not None:
            self._on_middle_click()

    def _right_click(self, event: "tk.Event") -> None:
        if self._on_right_click is not None:
            self._on_right_click(event.x_root, event.y_root)

    def _wheel(self, event: "tk.Event") -> str:
        """Mouse wheel over a tab scrolls the tab strip itself."""
        canvas = getattr(self.master, "master", None)
        if isinstance(canvas, tk.Canvas):
            try:
                canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
            except tk.TclError:  # pragma: no cover
                pass
        return "break"

    def _hover(self, entering: bool) -> None:
        if self.selected:
            return
        try:
            self.body.configure(fg_color=self.palette.hover if entering else "transparent")
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------- state
    def set_selected(self, selected: bool) -> None:
        """Highlight or unhighlight the tab."""
        self.selected = bool(selected)
        self._apply_style()

    def _apply_style(self) -> None:
        palette = self.palette
        try:
            self.body.configure(fg_color=palette.editor_bg if self.selected else "transparent")
            self.title.configure(text_color=palette.text if self.selected else palette.text_dim)
        except tk.TclError:  # pragma: no cover
            pass

    def set_dirty(self, dirty: bool) -> None:
        """Show/hide the unsaved dot."""
        if bool(dirty) == self._dirty_shown:
            return
        self._dirty_shown = bool(dirty)
        try:
            if dirty:
                self.dirty_label.pack(side="left", padx=(8, 0), before=self.title)
            else:
                self.dirty_label.pack_forget()
        except tk.TclError:  # pragma: no cover
            pass

    def set_name(self, name: str) -> None:
        try:
            self.title.configure(text=name)
        except tk.TclError:  # pragma: no cover
            pass

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin after a theme change."""
        self.palette = palette
        try:
            self.dirty_label.configure(text_color=palette.warning)
            self.close_button.configure(hover_color=palette.hover, text_color=palette.text_muted)
        except tk.TclError:  # pragma: no cover
            pass
        self._apply_style()


class EditorTabs(ctk.CTkFrame):
    """Tab strip + stacked editors for the current project."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        settings: Any = None,
        on_project_changed: Optional[Callable[[], Any]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
        on_active_changed: Optional[Callable[[Optional[Path]], Any]] = None,
        on_new_file: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self.palette = palette
        self._settings = settings
        self._on_project_changed = on_project_changed
        self._on_status = on_status
        self._on_active_changed = on_active_changed
        self._on_new_file = on_new_file
        self._log = get_logger("ui.tabs")

        self.project: Optional[Project] = None
        self.documents: dict[Path, EditorDocument] = {}
        self.editors: dict[Path, CodeEditor] = {}
        self.tabs: dict[Path, TabButton] = {}
        self.order: list[Path] = []
        self.current: Optional[Path] = None
        self._ask_unsaved: Optional[Callable[[EditorDocument], str]] = None

        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        strip = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=0, height=36)
        strip.grid(row=0, column=0, sticky="ew")
        strip.grid_propagate(False)
        strip.grid_rowconfigure(0, weight=1)
        strip.grid_columnconfigure(0, weight=1)

        self.tab_canvas = tk.Canvas(strip, height=36, bg=palette.panel_bg, highlightthickness=0, bd=0)
        self.tab_canvas.grid(row=0, column=0, sticky="nsew")
        self.tab_inner = tk.Frame(self.tab_canvas, bg=palette.panel_bg)
        self._canvas_window = self.tab_canvas.create_window((0, 0), window=self.tab_inner, anchor="nw")
        self.tab_canvas.bind("<Configure>", self._on_canvas_resize, add=True)
        self.tab_inner.bind("<Configure>", lambda event: self._update_scroll_region(), add=True)
        self.tab_canvas.bind("<MouseWheel>", self._on_strip_wheel, add=True)
        self.tab_canvas.bind("<Button-4>", lambda event: self._strip_scroll(-1), add=True)
        self.tab_canvas.bind("<Button-5>", lambda event: self._strip_scroll(1), add=True)

        self.add_button = ctk.CTkButton(
            strip, text="+", width=28, height=26, corner_radius=6,
            fg_color="transparent", hover_color=palette.hover, text_color=palette.text_dim,
            font=(palette.font_family, 15), command=self._new_file_clicked,
        )
        self.add_button.grid(row=0, column=1, sticky="e", padx=(2, 6))

        self.stack = ctk.CTkFrame(self, fg_color=palette.editor_bg, corner_radius=0)
        self.stack.grid(row=1, column=0, sticky="nsew")
        self.stack.grid_rowconfigure(0, weight=1)
        self.stack.grid_columnconfigure(0, weight=1)

        self.placeholder = ctk.CTkFrame(self.stack, fg_color=palette.editor_bg)
        ctk.CTkLabel(self.placeholder, text="No file open", font=(palette.font_family, 15, "bold"),
                     text_color=palette.text_dim).pack(pady=(90, 6))
        ctk.CTkLabel(
            self.placeholder,
            text="Create a project (Ctrl+Shift+N) or open an existing one (Ctrl+O),\n"
                 "then double-click a file in the explorer to edit it.",
            font=(palette.font_family, 11), text_color=palette.text_muted, justify="center",
        ).pack()

    def _on_canvas_resize(self, event: "tk.Event") -> None:
        try:
            self.tab_canvas.itemconfigure(self._canvas_window, width=max(event.width, 1))
        except tk.TclError:  # pragma: no cover
            pass
        self._update_scroll_region()

    def _update_scroll_region(self) -> None:
        try:
            bbox = self.tab_canvas.bbox("all") or (0, 0, 0, 0)
            self.tab_canvas.configure(scrollregion=bbox)
        except tk.TclError:  # pragma: no cover
            pass

    def _on_strip_wheel(self, event: "tk.Event") -> str:
        return self._strip_scroll(-1 if getattr(event, "delta", 0) > 0 else 1)

    def _strip_scroll(self, direction: int) -> str:
        try:
            self.tab_canvas.xview_scroll(direction, "units")
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    # -------------------------------------------------------------- open/close
    def set_project(self, project: Optional[Project], *, reopen: bool = True) -> None:
        """Switch to *project*: close old tabs, optionally restore saved tabs."""
        self.close_all(force=True)
        self.project = project
        if project is None or not reopen:
            self._refresh_visibility()
            return
        for relative in list(project.manifest.open_files):
            try:
                self.open_file(project.resolve(relative), activate=False)
            except (ProjectError, OSError):  # pragma: no cover
                continue
        self._refresh_visibility()

    def open_file(self, path: os.PathLike[str] | str, *, activate: bool = True) -> Optional[CodeEditor]:
        """Open *path* in a tab (reusing an existing tab when already open)."""
        try:
            key = Path(path).expanduser().resolve()
        except OSError:  # pragma: no cover
            key = Path(str(path)).expanduser()
        if key in self.editors:
            if activate:
                self.select(key)
            return self.editors[key]
        if not key.exists():
            self._status(f"{key.name} does not exist")
            return None
        if key.is_dir():
            self._status(f"{key.name} is a folder")
            return None
        document = EditorDocument(path=key)
        _changed, message = document.reload_from_disk()
        if message:
            self._status(message)
            return None
        if key.stat().st_size > 1_500_000:
            document.read_only = True
            self._status(f"{key.name} is large - opened read-only")
        editor = CodeEditor(
            self.stack, self.palette, document=document, settings=self._settings,
            on_dirty_changed=lambda dirty, k=key: self._editor_dirty(k, dirty),
            on_save_requested=lambda k=key: self.save_file(k),
            on_jump_to_error=self._jump_to_error,
        )
        self.documents[key] = document
        self.editors[key] = editor
        self.order.append(key)
        self.tabs[key] = TabButton(
            self.tab_inner, self.palette, document,
            on_select=lambda k=key: self.select(k),
            on_close=lambda k=key: self.close_file(k),
            on_middle_click=lambda k=key: self.close_file(k),
            on_right_click=lambda x, y, k=key: self._tab_menu(k, x, y),
        )
        self.tabs[key].pack(side="left", padx=(3, 0), pady=3)
        self._update_scroll_region()
        if activate:
            self.select(key)
        else:
            self._refresh_visibility()
        self._remember_open_files()
        return editor

    def select(self, path: os.PathLike[str] | str) -> bool:
        """Show the editor for *path* (``False`` when it is not open)."""
        try:
            key = Path(path).expanduser().resolve()
        except OSError:  # pragma: no cover
            key = Path(str(path))
        if key not in self.editors:
            return False
        self.current = key
        self._refresh_visibility()
        try:
            self.editors[key].focus_editor()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if self._on_active_changed is not None:
            self._on_active_changed(key)
        return True

    def selected_editor(self) -> Optional[CodeEditor]:
        """The visible editor (or ``None``)."""
        if self.current is None:
            return None
        return self.editors.get(self.current)

    @property
    def current_editor(self) -> Optional[CodeEditor]:
        return self.selected_editor()

    def editor_for(self, path: os.PathLike[str] | str) -> Optional[CodeEditor]:
        try:
            key = Path(path).expanduser().resolve()
        except OSError:  # pragma: no cover
            key = Path(str(path))
        return self.editors.get(key)

    def document_for(self, path: os.PathLike[str] | str) -> Optional[EditorDocument]:
        editor = self.editor_for(path)
        return editor.document if editor is not None else None

    def iter_editors(self) -> Iterator[CodeEditor]:
        for key in self.order:
            editor = self.editors.get(key)
            if editor is not None:
                yield editor

    def open_paths(self) -> list[Path]:
        return list(self.order)

    def _refresh_visibility(self) -> None:
        """Grid only the active editor, and update tab highlighting."""
        has_editors = bool(self.editors)
        for key, editor in self.editors.items():
            try:
                if key == self.current:
                    editor.grid(row=0, column=0, sticky="nsew")
                    editor.lift()
                else:
                    editor.grid_forget()
            except tk.TclError:  # pragma: no cover
                continue
        for key, tab in self.tabs.items():
            tab.set_selected(key == self.current)
        try:
            if has_editors and self.current in self.editors:
                self.placeholder.grid_forget()
            else:
                self.placeholder.grid(row=0, column=0, sticky="nsew")
        except tk.TclError:  # pragma: no cover
            pass

    def cycle(self, step: int) -> str:
        """Ctrl+PageUp / Ctrl+PageDown - move through the tabs."""
        if not self.order:
            return "break"
        try:
            index = self.order.index(self.current) if self.current in self.order else 0
        except ValueError:  # pragma: no cover
            index = 0
        self.select(self.order[(index + int(step)) % len(self.order)])
        return "break"

    # -------------------------------------------------------------- dirty/save
    def _editor_dirty(self, key: Path, dirty: bool) -> None:
        tab = self.tabs.get(key)
        if tab is not None:
            tab.set_dirty(bool(dirty))
        if self._settings is not None:
            try:
                self._settings.mark_dirty()
            except Exception:  # pragma: no cover
                pass
        if self.project is not None:
            try:
                self.project.save_manifest()
            except OSError:  # pragma: no cover
                pass

    @property
    def dirty_files(self) -> list[Path]:
        return [key for key, document in self.documents.items() if document.dirty]

    @property
    def has_unsaved(self) -> bool:
        return any(document.dirty for document in self.documents.values())

    def save_current(self) -> bool:
        if self.current is None:
            return False
        return self.save_file(self.current)

    def save_file(self, path: os.PathLike[str] | str) -> bool:
        """Write one buffer to disk."""
        try:
            key = Path(path).expanduser().resolve()
        except OSError:  # pragma: no cover
            key = Path(str(path))
        editor = self.editors.get(key)
        document = self.documents.get(key)
        if editor is None or document is None:
            return False
        if document.read_only:
            self._status(f"{document.name} is read-only")
            return False
        content = self._apply_save_transforms(editor.content())
        ok, message = document.save(content)
        tab = self.tabs.get(key)
        if ok:
            editor.mark_saved()
            if tab is not None:
                tab.set_dirty(False)
            self._status(f"Saved {document.name}")
            self._after_save(document)
        else:
            self._status(message or "save failed")
        return ok

    def _apply_save_transforms(self, content: str) -> str:
        settings = self._settings
        if settings is None:
            return content
        if getattr(settings, "trim_trailing_ws_on_save", False):
            content = "\n".join(line.rstrip() for line in content.split("\n"))
        if getattr(settings, "ensure_final_newline_on_save", True) and content and not content.endswith("\n"):
            content += "\n"
        return content

    def _after_save(self, document: EditorDocument) -> None:
        """Post-save hooks: JSON lint + let the app refresh (library imports)."""
        if document.is_json:
            import json

            try:
                json.loads(document.content or "{}")
            except ValueError as exc:
                self._status(f"{document.name}: invalid JSON - {exc}")
        if self._on_project_changed is not None:
            self._on_project_changed()

    def save_all(self) -> int:
        """Save every dirty buffer; returns how many files were written."""
        saved = 0
        for key in list(self.order):
            document = self.documents.get(key)
            if document is not None and document.dirty and self.save_file(key):
                saved += 1
        if saved:
            self._status(f"saved {saved} file(s)")
        return saved

    # ------------------------------------------------------------------- close
    def set_unsaved_handler(self, handler: Callable[[EditorDocument], str]) -> None:
        """Install the app's "Save / Don't save / Cancel" prompt."""
        self._ask_unsaved = handler

    def close_file(self, path: os.PathLike[str] | str, *, force: bool = False) -> bool:
        """Close one tab, prompting about unsaved changes unless *force*."""
        try:
            key = Path(path).expanduser().resolve()
        except OSError:  # pragma: no cover
            key = Path(str(path))
        editor = self.editors.get(key)
        document = self.documents.get(key)
        if editor is None or document is None:
            return False
        if document.dirty and not force:
            answer = self._confirm_close(document)
            if answer == "cancel":
                return False
            if answer == "save" and not self.save_file(key):
                return False
        try:
            editor.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        tab = self.tabs.pop(key, None)
        if tab is not None:
            try:
                tab.destroy()
            except tk.TclError:  # pragma: no cover
                pass
        self.editors.pop(key, None)
        self.documents.pop(key, None)
        if key in self.order:
            self.order.remove(key)
        if self.current == key:
            self.current = self.order[-1] if self.order else None
        self._refresh_visibility()
        self._remember_open_files()
        return True

    def close_others(self, path: Path) -> int:
        count = 0
        for key in [k for k in self.order if k != path]:
            if self.close_file(key):
                count += 1
        return count

    def close_all(self, force: bool = False) -> int:
        count = 0
        for key in list(self.order):
            if self.close_file(key, force=force):
                count += 1
        self.order.clear()
        self.current = None
        return count

    def _confirm_close(self, document: EditorDocument) -> str:
        """``"save" | "discard" | "cancel"`` for a dirty buffer."""
        if self._ask_unsaved is not None:
            return self._ask_unsaved(document)
        from .widgets.dialogs import ask_message

        answer = ask_message(
            self,
            kind="warning",
            title="Unsaved changes",
            message=f"{document.name} has unsaved changes.",
            detail="Save before closing? Otherwise the changes are lost.",
            buttons=("Cancel", "Don't Save", "Save"),
            palette=self.palette,
        )
        return {"Save": "save", "Don't Save": "discard"}.get(answer or "", "cancel")

    # ------------------------------------------------------------------- menus
    def _tab_menu(self, key: Path, x: int, y: int) -> None:
        menu = tk.Menu(self, tearoff=0)
        document = self.documents.get(key)
        name = document.name if document is not None else key.name
        menu.add_command(label=f"Save {name}", command=lambda: self.save_file(key),
                         state="normal" if document is not None and document.dirty else "disabled")
        menu.add_command(label="Save all", command=self.save_all)
        menu.add_separator()
        menu.add_command(label="Close", command=lambda: self.close_file(key))
        menu.add_command(label="Close others", command=lambda: self.close_others(key))
        menu.add_command(label="Close all", command=self.close_all)
        menu.add_separator()
        menu.add_command(label="Revert to saved", command=lambda: self._revert(key))
        menu.add_command(label="Copy full path", command=lambda: self._copy_text(str(key)))
        menu.add_command(label="Copy file name", command=lambda: self._copy_text(name))
        if self.project is not None:
            menu.add_command(label="Show in Explorer",
                             command=lambda: self._reveal(key))
        try:
            menu.tk_popup(x, y)
        finally:  # pragma: no cover
            menu.grab_release()

    def _copy_text(self, value: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
        except tk.TclError:  # pragma: no cover
            pass

    def _reveal(self, path: Path) -> None:
        from ..core.utils import open_path_in_file_manager

        open_path_in_file_manager(path)

    def _revert(self, key: Path) -> None:
        editor = self.editors.get(key)
        document = self.documents.get(key)
        if editor is None or document is None:
            return
        if document.dirty and not self.save_file(key):
            return
        _changed, message = document.reload_from_disk()
        if message:
            self._status(message)
            return
        editor.replace_all_text(document.content)
        editor.mark_saved()
        tab = self.tabs.get(key)
        if tab is not None:
            tab.set_dirty(False)
        self._status(f"reverted {document.name}")

    def _new_file_clicked(self) -> None:
        if self.project is None:
            self._status("open a project first")
            return
        if self._on_new_file is not None:
            self._on_new_file()

    # ------------------------------------------------------------------- sync
    def _jump_to_error(self, path_text: str, line: int) -> None:
        """Open the file a console diagnostic refers to (double-click a line)."""
        target: Optional[Path] = None
        if self.project is not None:
            try:
                candidate = self.project.resolve(path_text)
            except ProjectError:
                candidate = Path(path_text)
            if candidate.is_file():
                target = candidate
            else:
                target = next((p for p in self.project.root.rglob(Path(path_text).name) if p.is_file()), None)
        if target is None:
            target = Path(path_text)
        editor = self.open_file(target)
        if editor is not None:
            editor.goto_line(max(1, int(line or 1)))

    def rename_file(self, old_path: os.PathLike[str] | str, new_path: os.PathLike[str] | str) -> None:
        """Re-point a tab after the file was renamed on disk."""
        try:
            old = Path(old_path).expanduser().resolve()
            new = Path(new_path).expanduser().resolve()
        except OSError:  # pragma: no cover
            old, new = Path(str(old_path)), Path(str(new_path))
        if old not in self.editors or old == new:
            return
        editor = self.editors.pop(old)
        document = self.documents.pop(old)
        tab = self.tabs.pop(old, None)
        content = editor.content()
        try:
            editor.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        if tab is not None:
            try:
                tab.destroy()
            except tk.TclError:  # pragma: no cover
                pass
        index = self.order.index(old) if old in self.order else len(self.order)
        self.order.remove(old)
        self.order.insert(min(index, len(self.order)), new)
        document.path = new
        document.content = content
        document.dirty = new.exists() and new.read_text(encoding="utf-8", errors="replace") != content
        self.editors[new] = editor
        self.documents[new] = document
        self.tabs[new] = TabButton(
            self.tab_inner, self.palette, document,
            on_select=lambda k=new: self.select(k),
            on_close=lambda k=new: self.close_file(k),
            on_middle_click=lambda k=new: self.close_file(k),
            on_right_click=lambda x, y, k=new: self._tab_menu(k, x, y),
        )
        self.tabs[new].pack(side="left", padx=(3, 0), pady=3)
        self.tabs[new].set_dirty(document.dirty)
        editor.document = document
        self.current = new
        self._refresh_visibility()
        self._update_scroll_region()
        self._remember_open_files()

    def forget_path(self, path: os.PathLike[str] | str) -> None:
        """Drop the tab of a file that was deleted from disk."""
        self.close_file(path, force=True)

    def _remember_open_files(self) -> None:
        if self.project is None:
            return
        try:
            self.project.manifest.open_files = [
                relative for relative in (self._relative(key) for key in self.order) if relative
            ]
            self.project.save_manifest()
        except (ProjectError, OSError):  # pragma: no cover
            pass

    def _relative(self, path: Path) -> str:
        if self.project is None:
            return ""
        try:
            return str(self.project.relative(path))
        except ProjectError:
            return ""

    # --------------------------------------------------------------- external
    def check_external_changes(self) -> list[Path]:
        """Reload clean buffers whose file changed on disk; returns those files."""
        changed: list[Path] = []
        for key, document in list(self.documents.items()):
            try:
                if not key.is_file() or document.dirty:
                    continue
                if abs(key.stat().st_mtime - document.mtime) > 0.5:
                    changed.append(key)
            except OSError:  # pragma: no cover
                continue
        for key in changed:
            document = self.documents[key]
            _changed, message = document.reload_from_disk()
            if message:
                continue
            editor = self.editors.get(key)
            if editor is not None:
                editor.replace_all_text(document.content)
                editor.mark_saved()
                tab = self.tabs.get(key)
                if tab is not None:
                    tab.set_dirty(False)
                self._status(f"reloaded {document.name} (changed on disk)")
        return changed

    def apply_editor_settings(self) -> None:
        """Push editor settings (font, tabs, wrap) into every open editor."""
        for editor in self.editors.values():
            try:
                editor.apply_settings()
            except tk.TclError:  # pragma: no cover
                continue

    def set_diagnostics_for_project(self, diagnostics: list[Any]) -> None:
        """Distribute compile diagnostics to the editors of the matching files."""
        by_file: dict[str, list[Any]] = {}
        for diagnostic in diagnostics:
            by_file.setdefault(str(diagnostic.file), []).append(diagnostic)
        for key, editor in self.editors.items():
            matches: list[Any] = []
            for name, items in by_file.items():
                if Path(name).name == key.name or name.endswith(key.name):
                    matches.extend(items)
            editor.set_diagnostics(matches)

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin tabs and editors (theme switch)."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
            self.tab_canvas.configure(bg=palette.panel_bg)
            self.tab_inner.configure(bg=palette.panel_bg)
            self.add_button.configure(hover_color=palette.hover, text_color=palette.text_dim)
        except tk.TclError:  # pragma: no cover
            pass
        for tab in self.tabs.values():
            tab.refresh_palette(palette)
        for editor in self.editors.values():
            editor.refresh_palette(palette)

    def autosave_dirty(self) -> int:
        """Save all modified files (Settings \u2192 autosave)."""
        return self.save_all()

    def _status(self, message: str) -> None:
        if self._on_status is not None:
            self._on_status(message)
