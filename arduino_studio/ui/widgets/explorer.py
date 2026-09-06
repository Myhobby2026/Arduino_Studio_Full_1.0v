"""Project explorer pane (file tree + context menu).

The tree is a ``tkinter.ttk.Treeview`` themed to match CustomTkinter: it gives
keyboard navigation, incremental updates and a genuine hierarchy for free.
Every mutation is delegated to the app (which uses
:class:`arduino_studio.core.project.ProjectManager`), so the tree and
``project.json`` can never disagree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Iterator, Optional

import customtkinter as ctk

from ...core.project import Project, ProjectError, ProjectFile
from ...core.utils import SOURCE_SUFFIXES, get_logger, open_path_in_file_manager
from ..theme import Palette

__all__ = ["ProjectExplorer"]


#: Short monospace badge shown in front of a file name (no icon font needed).
_BADGES = {
    ".ino": "SKT",
    ".h": "H",
    ".hpp": "HPP",
    ".cpp": "CPP",
    ".c": "C",
    ".json": "CFG",
    ".txt": "TXT",
    ".md": "MD",
}

_TAG_BY_SUFFIX = {
    ".ino": "ino",
    ".h": "header",
    ".hpp": "header",
    ".cpp": "source",
    ".c": "source",
    ".cc": "source",
    ".json": "json",
    ".txt": "text",
    ".md": "text",
}


class ProjectExplorer(ctk.CTkFrame):
    """Left-hand project navigation pane."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        on_open_file: Optional[Callable[[Path], Any]] = None,
        on_new_file: Optional[Callable[[str], Any]] = None,
        on_new_folder: Optional[Callable[[str], Any]] = None,
        on_rename: Optional[Callable[[Path, str], Any]] = None,
        on_delete: Optional[Callable[[Path], Any]] = None,
        on_duplicate: Optional[Callable[[Path], Any]] = None,
        on_import_file: Optional[Callable[[], Any]] = None,
        on_refresh: Optional[Callable[[], Any]] = None,
        on_project_action: Optional[Callable[[str], Any]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.panel_bg, corner_radius=0,
                         border_width=1, border_color=palette.border)
        self.palette = palette
        self._on_open_file = on_open_file
        self._on_new_file = on_new_file
        self._on_new_folder = on_new_folder
        self._on_rename = on_rename
        self._on_delete = on_delete
        self._on_duplicate = on_duplicate
        self._on_import_file = on_import_file
        self._on_refresh = on_refresh
        self._on_project_action = on_project_action
        self._on_status = on_status
        self._log = get_logger("ui.explorer")

        self.project: Optional[Project] = None
        self._node_by_id: dict[str, ProjectFile] = {}
        self._id_by_path: dict[str, str] = {}
        self._expanded: set[str] = set()
        self._filter = ""
        self._dirty_paths: set[str] = set()

        self._build()
        self.set_empty_state()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        header.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(header, text="PROJECT", font=(palette.font_family, 11, "bold"),
                                        text_color=palette.text, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.refresh_button = ctk.CTkButton(header, text="\u21bb", width=26, height=24, corner_radius=5,
                                            fg_color="transparent", hover_color=palette.hover,
                                            text_color=palette.text_dim, command=self.refresh)
        self.refresh_button.grid(row=0, column=3, sticky="e", padx=(4, 0))
        self.new_button = ctk.CTkButton(header, text="+", width=26, height=24, corner_radius=5,
                                        fg_color="transparent", hover_color=palette.hover,
                                        text_color=palette.text_dim, font=(palette.font_family, 14),
                                        command=self._new_file_clicked)
        self.new_button.grid(row=0, column=2, sticky="e")
        self.menu_button = ctk.CTkButton(header, text="\u2261", width=26, height=24, corner_radius=5,
                                         fg_color="transparent", hover_color=palette.hover,
                                         text_color=palette.text_dim, command=self._header_menu_clicked)
        self.menu_button.grid(row=0, column=1, sticky="e", padx=(6, 0))

        self.filter_entry = ctk.CTkEntry(self, placeholder_text="filter files (Ctrl+F here)",
                                         height=26, font=(palette.font_family, 10),
                                         fg_color=palette.surface, text_color=palette.text,
                                         border_color=palette.border, corner_radius=6)
        self.filter_entry.grid(row=1, column=0, sticky="ew", padx=8, pady=(6, 4))
        self.filter_entry.bind("<KeyRelease>", lambda event: self._apply_filter(), add=True)
        self.filter_entry.bind("<Escape>", lambda event: self._clear_filter(), add=True)

        body = ctk.CTkFrame(self, fg_color=palette.editor_bg, corner_radius=8,
                            border_width=1, border_color=palette.border)
        body.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 2))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(body, show="tree", selectmode="browse", style="Studio.Treeview",
                                 height=18, takefocus=1)
        self.tree.column("#0", width=220, min_width=120, anchor="w", stretch=True)
        self.tree_scroll = ctk.CTkScrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=3)
        self.tree_scroll.grid(row=0, column=1, sticky="ns", padx=(2, 4), pady=3)
        self._tag_colours()

        self.tree.bind("<Double-1>", lambda event: self._activate_clicked(event), add=True)
        self.tree.bind("<Return>", lambda event: self._activate_clicked(event), add=True)
        self.tree.bind("<Button-1>", self._on_clicked, add=True)
        self.tree.bind("<Button-3>", self._on_right_click, add=True)
        self.tree.bind("<F2>", lambda event: self._rename_clicked(), add=True)
        self.tree.bind("<Delete>", lambda event: self._delete_clicked(), add=True)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
        footer.grid_columnconfigure(1, weight=1)
        self.count_label = ctk.CTkLabel(footer, text="", font=(palette.font_family, 9),
                                        text_color=palette.text_muted, anchor="w")
        self.count_label.grid(row=0, column=0, sticky="w")
        self.hint_label = ctk.CTkLabel(footer, text="F2 rename \u00b7 Del delete \u00b7 right-click menu",
                                       font=(palette.font_family, 9), text_color=palette.text_muted,
                                       anchor="e")
        self.hint_label.grid(row=0, column=1, sticky="e")

    def _tag_colours(self) -> None:
        """(Re)apply tree colours for the current palette."""
        palette = self.palette
        colours = {
            "dir": palette.text,
            "ino": palette.accent,
            "header": palette.warning,
            "source": palette.success,
            "json": palette.info,
            "text": palette.text_dim,
            "other": palette.text_muted,
        }
        for name, colour in colours.items():
            try:
                self.tree.tag_configure(name, foreground=colour)
            except tk.TclError:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ state
    def set_empty_state(self, message: str = "") -> None:
        """Clear the pane and show a hint."""
        self.project = None
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:  # pragma: no cover
            pass
        self._node_by_id.clear()
        self._id_by_path.clear()
        self.title_label.configure(text="PROJECT")
        self.count_label.configure(text=message or "no project open")
        self.hint_label.configure(text="File \u2192 New project (Ctrl+Shift+N)")

    def set_project(self, project: Optional[Project]) -> None:
        """Attach *project* (or clear) and rebuild the tree."""
        if project is None:
            self.set_empty_state()
            return
        self.project = project
        self.title_label.configure(text=project.name.upper())
        self.refresh()

    def refresh(self, *, keep_expansion: bool = True) -> None:
        """Rebuild the tree from the project directory."""
        if self.project is None:
            self.set_empty_state()
            return
        if keep_expansion:
            try:
                self._expanded = {item for item in self.tree.get_children() if self.tree.item(item, "open")}
                self._expanded |= {child for child in self._walk() if self.tree.item(child, "open")}
            except tk.TclError:  # pragma: no cover
                pass
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:  # pragma: no cover
            pass
        self._node_by_id.clear()
        self._id_by_path.clear()
        file_count = 0
        for node in self.project.tree():
            file_count += self._insert_node("", node)
        self.count_label.configure(text=f"{file_count} file(s) \u00b7 {self.project.name}")
        self._apply_filter()

    def _insert_node(self, parent_id: str, node: ProjectFile) -> int:
        """Recursive tree fill; returns the number of files added below *node*."""
        tag = _TAG_BY_SUFFIX.get(node.suffix, "dir" if node.is_dir else "other")
        if node.is_dir:
            label = f"{node.name}/"
        else:
            badge = _BADGES.get(node.suffix)
            label = f"[{badge}]  {node.name}" if badge else f"        {node.name}"
        if not node.is_dir and str(node.path) in self._dirty_paths:
            label = f"{label}  \u25cf"
            tag = f"{tag} dirty"
        try:
            item = self.tree.insert(parent_id, "end", text=label, tags=(tag,))
        except tk.TclError:  # pragma: no cover
            return 0
        self._node_by_id[item] = node
        if not node.is_dir:
            self._id_by_path[str(node.path)] = item
        count = 0 if node.is_dir else 1
        for child in node.children:
            count += self._insert_node(item, child)
        if node.is_dir and (not parent_id or str(node.path) in self._expanded or count):
            try:
                self.tree.item(item, open=True)
            except tk.TclError:  # pragma: no cover
                pass
        return count

    def set_dirty_paths(self, paths: Any) -> None:
        """Remember which files have unsaved changes (shown as a dot)."""
        self._dirty_paths = {str(p) for p in paths}
        self.refresh()

    def select_path(self, path: os.PathLike[str] | str) -> bool:
        """Scroll to and highlight the row for *path*."""
        item = self._id_by_path.get(str(Path(str(path)).expanduser()))
        if item is None:
            return False
        try:
            self.tree.selection_set(item)
            self.tree.see(item)
            parent = self.tree.parent(item)
            while parent:
                self.tree.item(parent, open=True)
                parent = self.tree.parent(parent)
        except tk.TclError:  # pragma: no cover
            return False
        return True

    def selected_path(self) -> Optional[Path]:
        """Currently highlighted row's path (or ``None``)."""
        try:
            selection = self.tree.selection()
        except tk.TclError:  # pragma: no cover
            return None
        if not selection:
            return None
        node = self._node_by_id.get(selection[0])
        return node.path if node is not None else None

    def selected_node(self) -> Optional[ProjectFile]:
        try:
            selection = self.tree.selection()
        except tk.TclError:  # pragma: no cover
            return None
        return self._node_by_id.get(selection[0]) if selection else None

    def target_folder(self) -> Optional[Path]:
        """Folder the toolbar/menu actions should create files in."""
        node = self.selected_node()
        if self.project is None:
            return None
        if node is not None:
            if node.is_dir:
                return node.path
            return node.path.parent
        return self.project.root

    # ------------------------------------------------------------------ events
    def _on_clicked(self, event: "tk.Event") -> str:
        try:
            row = self.tree.identify_row(event.y)
        except tk.TclError:  # pragma: no cover
            row = ""
        if row:
            try:
                self.tree.selection_set(row)
            except tk.TclError:  # pragma: no cover
                pass
        return "break"

    def _activate_clicked(self, event: Any = None) -> str:
        """Double-click / Enter: expand a folder or open a file."""
        node = self.selected_node()
        if node is None:
            return "break"
        if node.is_dir:
            try:
                item = self.tree.selection()[0]
                self.tree.item(item, open=not self.tree.item(item, "open"))
            except (tk.TclError, IndexError):  # pragma: no cover
                pass
            return "break"
        if node.suffix.lower() not in SOURCE_SUFFIXES:
            self._status(f"{node.name} is not a source file - use 'Open with Windows editor'")
            return "break"
        if self._on_open_file is not None:
            self._on_open_file(node.path)
        return "break"

    def _on_right_click(self, event: "tk.Event") -> str:
        try:
            row = self.tree.identify_row(event.y)
            if row:
                self.tree.selection_set(row)
        except tk.TclError:  # pragma: no cover
            row = ""
        menu = self._build_menu(row)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:  # pragma: no cover
            menu.grab_release()
        return "break"

    def _header_menu_clicked(self) -> None:
        menu = self._build_menu(self.tree.selection()[0] if self.tree.selection() else "")
        try:
            menu.tk_popup(self.menu_button.winfo_rootx(),
                          self.menu_button.winfo_rooty() + self.menu_button.winfo_height())
        finally:  # pragma: no cover
            menu.grab_release()

    def _build_menu(self, item: str) -> tk.Menu:
        """Context menu for *item* (empty = project root actions)."""
        menu = tk.Menu(self, tearoff=0)
        node = self._node_by_id.get(item) if item else None
        path = node.path if node is not None else (self.project.root if self.project else None)
        is_dir = bool(node.is_dir) if node is not None else True
        if node is not None:
            menu.add_command(label=f"Open {node.name}" if not is_dir else "Expand / collapse",
                             command=self._activate_clicked,
                             state="normal" if not is_dir else "disabled")
            menu.add_command(label="Rename  (F2)", command=self._rename_clicked,
                             state="normal" if node is not None and path != self._root_path() else "disabled")
            menu.add_command(label="Duplicate", command=self._duplicate_clicked,
                             state="normal" if not is_dir else "disabled")
            menu.add_command(label="Delete  (Del)", command=self._delete_clicked,
                             state="normal" if path != self._root_path() else "disabled")
            menu.add_separator()
            menu.add_command(label="Copy full path", command=lambda: self._copy_text(str(path)))
            menu.add_command(label="Copy relative path", command=lambda: self._copy_text(self._relative(path)))
            menu.add_command(label="Show in Explorer", command=lambda: self._reveal(path))
            menu.add_command(label="Open with Windows editor", command=lambda: self._open_external(path))
            menu.add_separator()
        menu.add_command(label="New file\u2026", command=self._new_file_clicked)
        menu.add_command(label="New folder\u2026", command=self._new_folder_clicked)
        menu.add_command(label="Add existing file\u2026", command=lambda: self._callback(self._on_import_file))
        menu.add_separator()
        menu.add_command(label="Refresh", command=self.refresh)
        if self.project is not None:
            menu.add_command(label="Show project in Explorer",
                             command=lambda: open_path_in_file_manager(self.project.root))
            menu.add_command(label="Open project folder in terminal",
                             command=lambda: self._project_action("terminal"))
            menu.add_command(label="Project properties\u2026",
                             command=lambda: self._project_action("properties"))
        return menu

    def _root_path(self) -> Optional[Path]:
        return self.project.root if self.project is not None else None

    def _relative(self, path: Optional[Path]) -> str:
        if self.project is None or path is None:
            return ""
        try:
            return str(self.project.relative(path))
        except ProjectError:
            return str(path)

    def _new_file_clicked(self) -> None:
        if self.project is None:
            self._status("open a project first")
            return
        if self._on_new_file is not None:
            folder = self.target_folder()
            self._on_new_file(str(folder) if folder is not None else str(self.project.root))

    def _new_folder_clicked(self) -> None:
        if self.project is None:
            self._status("open a project first")
            return
        if self._on_new_folder is not None:
            folder = self.target_folder()
            self._on_new_folder(str(folder) if folder is not None else str(self.project.root))

    def _rename_clicked(self) -> None:
        path = self.selected_path()
        if path is None or self.project is None:
            self._status("select a file or folder first")
            return
        if path == self.project.root:
            return
        if self._on_rename is not None:
            self._on_rename(path, path.name)

    def _delete_clicked(self) -> None:
        path = self.selected_path()
        if path is None or self.project is None:
            return
        if path == self.project.root:
            return
        if self._on_delete is not None:
            self._on_delete(path)

    def _duplicate_clicked(self) -> None:
        path = self.selected_path()
        if path is None or self._on_duplicate is None:
            return
        self._on_duplicate(path)

    def _copy_text(self, value: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
            self._status("copied to clipboard")
        except tk.TclError:  # pragma: no cover
            pass

    def _reveal(self, path: Optional[Path]) -> None:
        if path is not None:
            open_path_in_file_manager(path)

    def _open_external(self, path: Optional[Path]) -> None:
        """Open with whatever Windows has registered for the extension."""
        if path is None:
            return
        opener = "start" if os.name == "nt" else ("open" if sys.platform == "darwin" else "xdg-open")
        try:
            subprocess.Popen([opener, str(path)], shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:  # pragma: no cover
            self._status(f"could not open {path.name}: {exc}")

    def set_main_sketch(self, path: Path) -> None:
        """Point the project manifest at a different primary ``.ino``."""
        if self.project is None:
            return
        try:
            self.project.manifest.main_file = self.project.relative(path)
            self.project.save_manifest()
            self._status(f"main sketch: {path.name}")
            self.refresh()
        except (ProjectError, OSError) as exc:
            self._status(f"could not change main sketch: {exc}")

    def _project_action(self, action: str) -> None:
        if self._on_project_action is not None:
            self._on_project_action(action)

    def _callback(self, handler: Optional[Callable[[], Any]]) -> None:
        if handler is not None:
            handler()

    def _status(self, message: str) -> None:
        if self._on_status is not None:
            self._on_status(message)

    # ----------------------------------------------------------------- filter
    def _clear_filter(self) -> str:
        self.filter_entry.delete(0, "end")
        self._apply_filter()
        return "break"

    def _apply_filter(self) -> None:
        """Detach rows that do not match the filter text."""
        needle = self.filter_entry.get().strip().lower()
        self._filter = needle
        if self.project is None:
            return
        for item in list(self._walk()):
            node = self._node_by_id.get(item)
            if node is None:
                continue
            if not needle:
                self._attach(item)
                continue
            match = needle in node.name.lower() or needle in str(self._relative(node.path)).lower()
            if not match and node.is_dir:
                match = any(needle in child.name.lower() for child in node.children)
            if match:
                self._attach(item)
                parent = item
                try:
                    while self.tree.parent(parent):
                        parent = self.tree.parent(parent)
                        self._attach(parent)
                        self.tree.item(parent, open=True)
                except tk.TclError:  # pragma: no cover
                    pass
            else:
                try:
                    self.tree.detach(item)
                except tk.TclError:  # pragma: no cover
                    pass

    def _attach(self, item: str) -> None:
        try:
            if item not in self.tree.get_children(self.tree.parent(item) or ""):
                self.tree.reattach(item, self.tree.parent(item) or "", "end")
        except tk.TclError:  # pragma: no cover
            pass

    def _walk(self) -> Iterator[str]:
        """Depth-first walk of all visible rows."""
        try:
            stack = list(self.tree.get_children())
        except tk.TclError:  # pragma: no cover
            return
        while stack:
            item = stack.pop(0)
            yield item
            try:
                stack[0:0] = list(self.tree.get_children(item))
            except tk.TclError:  # pragma: no cover
                continue

    # ------------------------------------------------------------------- misc
    def focus_tree(self) -> None:
        try:
            self.tree.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def refresh_palette(self, palette: Palette) -> None:
        """Re-apply every colour after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.panel_bg, border_color=palette.border)
            self.title_label.configure(text_color=palette.text)
            for button in (self.refresh_button, self.new_button, self.menu_button):
                button.configure(hover_color=palette.hover, text_color=palette.text_dim)
            self.filter_entry.configure(fg_color=palette.surface, text_color=palette.text,
                                        border_color=palette.border)
            self.count_label.configure(text_color=palette.text_muted)
            self.hint_label.configure(text_color=palette.text_muted)
        except tk.TclError:  # pragma: no cover
            pass
        self._tag_colours()
