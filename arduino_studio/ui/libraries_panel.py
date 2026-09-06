"""Library Manager screen (search, install, update, uninstall, ZIP / Git).

Everything goes through :class:`arduino_studio.core.library_manager.LibraryManager`
in a background task lane, so the GUI never blocks on ``arduino-cli lib ...``.
The screen can also be asked to suggest libraries for missing ``#include``
directives (the compile error path) and to install project local libraries into
``<project>/libraries``.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.library_manager import LibraryManager, LibraryRecord
from ..core.runner import LANE_BUILD, LANE_QUERY, TaskResult
from ..core.utils import Debouncer, get_logger
from .theme import Palette
from .widgets.data_table import DataTable
from .widgets.dialogs import MessageDialog, ask_message, ask_plan, ask_text, ask_yes_no
from .widgets.text_view import ScrolledTextView, TagSpec

__all__ = ["LibrariesPanel"]

_COLUMNS = (
    ("name", "Library", 240, "w"),
    ("installed", "Installed", 96, "w"),
    ("latest", "Latest", 96, "w"),
    ("author", "Author", 170, "w"),
    ("summary", "Sentence", 340, "w"),
)

_MODES = ("Index search", "Installed", "Update available", "Project libraries")


class LibrariesPanel(ctk.CTkFrame):
    """Library manager UI (search box, results table, details, actions)."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        manager: LibraryManager,
        runner: Any,
        console: Any = None,
        settings: Any = None,
        get_project: Optional[Callable[[], Any]] = None,
        get_fqbn: Optional[Callable[[], str]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
        on_add_include: Optional[Callable[[str], Any]] = None,
        on_project_changed: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self.palette = palette
        self.manager = manager
        self._runner = runner
        self._console = console
        self._settings = settings
        self._get_project = get_project
        self._get_fqbn = get_fqbn
        self._on_status = on_status
        self._on_add_include = on_add_include
        self._on_project_changed = on_project_changed
        self._log = get_logger("ui.libraries")

        self._records: dict[str, LibraryRecord] = {}
        self._mode = "Index search"
        self._busy = False
        self._search_history: list[str] = []
        self._history_index = 0
        self._task = None

        self._build()
        self._debouncer = Debouncer(350, self._debounced_search, scheduler=self.after,
                                    canceller=self.after_cancel)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(header, text="Search", font=(palette.font_family, 10, "bold"),
                     text_color=palette.text_dim).grid(row=0, column=0, sticky="w", padx=(12, 6), pady=10)
        self.search_entry = ctk.CTkEntry(header, placeholder_text="Servo, DHT, FastLED, Adafruit GFX\u2026",
                                         height=30, font=(palette.font_family, 11), fg_color=palette.surface,
                                         text_color=palette.text, border_color=palette.border)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=10)
        self.search_entry.bind("<KeyRelease>", self._on_typing, add=True)
        self.search_entry.bind("<Return>", lambda event: self._run_search(), add=True)
        self.search_entry.bind("<Up>", lambda event: self._history_step(-1), add=True)
        self.search_entry.bind("<Down>", lambda event: self._history_step(1), add=True)
        self.search_button = ctk.CTkButton(header, text="Search", width=84, height=30, corner_radius=8,
                                           fg_color=palette.accent, hover_color=palette.accent_hover,
                                           text_color=palette.accent_text, command=self._run_search)
        self.search_button.grid(row=0, column=2, sticky="e", padx=(0, 6), pady=10)
        self.cancel_button = ctk.CTkButton(header, text="Cancel", width=70, height=30, corner_radius=8,
                                           fg_color="transparent", border_width=1, border_color=palette.border,
                                           hover_color=palette.hover, text_color=palette.text_dim,
                                           command=self._cancel_task)
        self.cancel_button.grid(row=0, column=3, sticky="e", padx=(0, 12), pady=10)

        self.mode_menu = ctk.CTkSegmentedButton(header, values=list(_MODES), height=28,
                                                font=(palette.font_family, 10), command=self._mode_changed)
        self.mode_menu.set(_MODES[0])
        self.mode_menu.grid(row=1, column=0, columnspan=2, sticky="w", padx=(12, 6), pady=(0, 10))

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=1, column=2, columnspan=2, sticky="e", padx=(0, 12), pady=(0, 10))
        self.index_button = ctk.CTkButton(actions, text="Update index", width=118, height=28, corner_radius=6,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text_dim,
                                          command=self.update_index)
        self.index_button.pack(side="left", padx=(0, 4))
        self.update_all_button = ctk.CTkButton(actions, text="Update all", width=100, height=28, corner_radius=6,
                                                fg_color="transparent", border_width=1, border_color=palette.border,
                                                hover_color=palette.hover, text_color=palette.text_dim,
                                                command=self.update_all_libraries)
        self.update_all_button.pack(side="left", padx=(0, 4))
        self.zip_button = ctk.CTkButton(actions, text="Add .ZIP", width=96, height=28, corner_radius=6,
                                        fg_color="transparent", border_width=1, border_color=palette.border,
                                        hover_color=palette.hover, text_color=palette.text_dim,
                                        command=self.install_from_zip)
        self.zip_button.pack(side="left", padx=(0, 4))
        self.git_button = ctk.CTkButton(actions, text="Add Git URL", width=104, height=28, corner_radius=6,
                                        fg_color="transparent", border_width=1, border_color=palette.border,
                                        hover_color=palette.hover, text_color=palette.text_dim,
                                        command=self.install_from_git)
        self.git_button.pack(side="left")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2, minsize=320)

        self.table = DataTable(
            body, palette, _COLUMNS, height=16, on_select=self._row_selected,
            on_activate=self._activate_row, on_right_click=self._row_menu,
            empty_text="Type a search term, or switch to 'Installed'",
        )
        self.table.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        details = ctk.CTkFrame(body, fg_color=palette.panel_bg, corner_radius=8)
        details.grid(row=0, column=1, sticky="nsew")
        details.grid_rowconfigure(1, weight=1)
        details.grid_columnconfigure(0, weight=1)

        self.detail_title = ctk.CTkLabel(details, text="No library selected",
                                         font=(palette.font_family, 13, "bold"), text_color=palette.text,
                                         anchor="w", justify="left", wraplength=380)
        self.detail_title.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        self.detail_view = ScrolledTextView(details, palette, readonly=True, wrap="word",
                                            bg=palette.surface, fg=palette.text,
                                            font=(palette.font_family, 10),
                                            tags=(TagSpec("head", foreground=palette.text, font_weight="bold"),
                                                  TagSpec("key", foreground=palette.text_muted),
                                                  TagSpec("value", foreground=palette.text),
                                                  TagSpec("url", foreground=palette.info, underline=True)))
        self.detail_view.grid(row=1, column=0, sticky="nsew", padx=(12, 4), pady=(0, 6))

        buttons = ctk.CTkFrame(details, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        buttons.grid_columnconfigure((0, 1, 2), weight=1)
        self.install_button = ctk.CTkButton(buttons, text="Install", height=32, corner_radius=8,
                                            fg_color=palette.accent, hover_color=palette.accent_hover,
                                            text_color=palette.accent_text, command=self.install_selected)
        self.install_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.uninstall_button = ctk.CTkButton(buttons, text="Uninstall", height=32, corner_radius=8,
                                              fg_color="transparent", border_width=1, border_color=palette.border,
                                              hover_color=palette.hover, text_color=palette.text_dim,
                                              command=self.uninstall_selected)
        self.uninstall_button.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        self.include_button = ctk.CTkButton(buttons, text="Add #include", height=32, corner_radius=8,
                                            fg_color="transparent", border_width=1, border_color=palette.border,
                                            hover_color=palette.hover, text_color=palette.text_dim,
                                            command=self.add_include_selected)
        self.include_button.grid(row=0, column=2, sticky="ew")
        scope_row = ctk.CTkFrame(details, fg_color="transparent")
        scope_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.project_scope = ctk.CTkCheckBox(scope_row, text="Install into this project's libraries/ folder",
                                             font=(palette.font_family, 10), text_color=palette.text_dim,
                                             checkbox_width=16, checkbox_height=16)
        self.project_scope.pack(side="left")
        self.version_menu = ctk.CTkOptionMenu(scope_row, values=["latest"], width=130, height=26,
                                              fg_color=palette.surface, button_color=palette.border,
                                              button_hover_color=palette.hover, text_color=palette.text,
                                              command=lambda value: None)
        self.version_menu.pack(side="right")
        ctk.CTkLabel(scope_row, text="Version", font=(palette.font_family, 9),
                     text_color=palette.text_muted).pack(side="right", padx=(0, 6))

        footer = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8, height=34)
        footer.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        footer.grid_propagate(False)
        footer.grid_columnconfigure(1, weight=1)
        self.status_label = ctk.CTkLabel(footer, text="Idle", font=(palette.font_family, 10),
                                         text_color=palette.text_dim, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self.progress = ctk.CTkProgressBar(footer, width=220, height=10)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        self.count_label = ctk.CTkLabel(footer, text="", font=(palette.font_family, 10),
                                        text_color=palette.text_muted, anchor="e")
        self.count_label.grid(row=0, column=2, sticky="e", padx=(0, 12))

    # ------------------------------------------------------------------ search
    def _on_typing(self, event: Any) -> str:
        key = getattr(event, "keysym", "")
        if key in {"Return", "Up", "Down", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"}:
            return ""
        self._debouncer.hit()
        return ""

    def _debounced_search(self) -> None:
        """Runs after the typing pause; skips the expensive index search when empty."""
        try:
            term = self.search_entry.get().strip()
        except tk.TclError:  # pragma: no cover
            return
        if self._mode != "Index search" or term:
            self._run_search()

    def _history_step(self, direction: int) -> str:
        """Arrow keys walk the previous searches."""
        if not self._search_history:
            return "break"
        self._history_index = max(0, min(len(self._search_history) - 1,
                                        getattr(self, "_history_index", len(self._search_history)) + int(direction)))
        index = self._history_index
        try:
            self.search_entry.delete(0, "end")
            self.search_entry.insert(0, self._search_history[index])
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    def _mode_changed(self, mode: Any) -> None:
        """Segmented control: switch list source and reload it."""
        self._mode = str(mode)
        self._run_search()

    def focus_search(self) -> None:
        """Menu / toolbar action: show the panel and focus the search box."""
        try:
            self.search_entry.focus_set()
        except tk.TclError:  # pragma: no cover
            pass
        if not self._records:
            self.refresh_installed()

    def refresh(self) -> None:
        """Reload whichever tab is active (called when the panel is shown)."""
        if self._mode == "Installed":
            self.refresh_installed()
        elif self._mode == "Update available":
            self.refresh_updates()
        elif self._mode == "Project libraries":
            self.refresh_project()
        else:
            term = ""
            try:
                term = self.search_entry.get().strip()
            except tk.TclError:  # pragma: no cover
                pass
            if term:
                self._run_search()
            else:
                self.refresh_installed()

    # ------------------------------------------------------------------- tasks
    def _submit(self, name: str, work: Callable[[Any], Any], *, lane: str = LANE_QUERY,
                on_done: Optional[Callable[[TaskResult], Any]] = None,
                busy_label: str = "") -> None:
        if self._busy:
            self._status("another library operation is running")
            return
        self._set_busy(True, busy_label or name)

        def wrapped(context: Any) -> Any:
            return work(context)

        def done(result: TaskResult) -> None:
            self._set_busy(False)
            if on_done is not None:
                on_done(result)

        try:
            self._task = self._runner.submit(name, wrapped, lane=lane, on_done=done,
                                             on_log=self._on_task_log)
        except Exception as exc:  # pragma: no cover - defensive
            self._set_busy(False)
            self._status(f"could not start {name}: {exc}")

    def _on_task_log(self, line: str, level: str) -> None:
        if self._console is not None:
            try:
                self._console.write(line, level)
            except (tk.TclError, AttributeError):  # pragma: no cover
                pass

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = bool(busy)
        state = "disabled" if busy else "normal"
        try:
            for widget in (self.search_button, self.index_button, self.update_all_button, self.zip_button,
                           self.git_button, self.install_button, self.uninstall_button, self.include_button):
                widget.configure(state=state)
            self.progress.configure(mode="indeterminate" if busy else "determinate")
            if busy:
                self.progress.start()
            else:
                self.progress.stop()
                self.progress.set(0)
            self.status_label.configure(text=label or ("Working\u2026" if busy else "Idle"))
        except tk.TclError:  # pragma: no cover
            pass

    def _cancel_task(self) -> None:
        task = self._task
        if task is not None and not task.is_done:
            task.cancel("cancelled from the library manager")
            self._status("cancellation requested")

    def _status(self, message: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(message)
            except Exception:  # pragma: no cover
                pass
        try:
            self.status_label.configure(text=message[:120])
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------- lists
    def _run_search(self) -> None:
        term = ""
        try:
            term = self.search_entry.get().strip()
        except tk.TclError:  # pragma: no cover
            pass
        if term and term not in self._search_history:
            self._search_history.append(term)
            self._history_index = len(self._search_history)
        mode = self._mode

        def work(context: Any) -> list[LibraryRecord]:
            if mode == "Installed":
                context.log("arduino-cli lib list", "dim")
                return self.manager.installed(include_builtins=True)
            if mode == "Update available":
                context.log("arduino-cli lib list --updatable", "dim")
                return self.manager.updatable(fqbn=self._fqbn())
            if mode == "Project libraries":
                project = self._project()
                if project is None:
                    return []
                context.log(f"scanning {Path(project.root) / 'libraries'}", "dim")
                return self.manager.project_libraries(project)
            if not term:
                return []
            context.log(f"arduino-cli lib search {term or ''}".strip(), "dim")
            return self.manager.search(term)

        def done(result: TaskResult) -> None:
            if not result.ok:
                self._show_error("Library search failed", result)
                return
            records = result.payload or []
            self._publish(records)
            if mode == "Index search" and not records and term:
                self._status(f"nothing found for '{term}' - try updating the index")
                if self._index_is_stale():
                    answer = ask_yes_no(self, "The library index has never been downloaded (or is old).",
                                       "Update the index now? This downloads the Index from the Arduino "
                                       "repository list and can take a minute.",
                                       yes_label="Update index", palette=self.palette)
                    if answer:
                        self.update_index()
            else:
                self._status(f"{len(records)} librar{'y' if len(records) == 1 else 'ies'} shown")

        self._submit(f"libraries:{mode}", work, on_done=done, busy_label=f"{mode.lower()}\u2026")

    def _publish(self, records: list[LibraryRecord]) -> None:
        self._records = {}
        rows: list[tuple[str, list[str]]] = []
        tags: dict[str, str] = {}
        for index, record in enumerate(records):
            key = record.name or f"lib{index}"
            self._records[key] = record
            rows.append((key, [record.display_name, record.version or "-", record.latest_version or "-",
                               record.author or "-", record.description[:110]]))
            tags[key] = self._tag_for(record)
        self.table.set_rows(rows, tags)
        try:
            self.count_label.configure(text=f"{len(records)} shown")
        except tk.TclError:  # pragma: no cover
            pass
        self._show_record(self.table.selected_key() or (rows[0][0] if rows else ""))

    def _tag_for(self, record: LibraryRecord) -> str:
        if record.update_available:
            return "update"
        if record.installed:
            return "installed"
        return "available"

    def refresh_installed(self) -> None:
        """Shortcut used by the app: show what is installed."""
        self._mode = "Installed"
        try:
            self.mode_menu.set("Installed")
        except tk.TclError:  # pragma: no cover
            pass
        self._run_search()

    def refresh_updates(self) -> None:
        self._mode = "Update available"
        try:
            self.mode_menu.set("Update available")
        except tk.TclError:  # pragma: no cover
            pass
        self._run_search()

    def refresh_project(self) -> None:
        self._mode = "Project libraries"
        try:
            self.mode_menu.set("Project libraries")
        except tk.TclError:  # pragma: no cover
            pass
        self._run_search()

    def _index_is_stale(self) -> bool:
        try:
            max_age = int(getattr(self._settings, "lib_index_max_age_days", 7) or 7)
            return self.manager.index_is_stale(max_age)
        except Exception:  # pragma: no cover
            return True

    # ------------------------------------------------------------------ detail
    def _row_selected(self, key: str) -> None:
        self._show_record(key)

    def _show_record(self, key: str) -> None:
        record = self._records.get(key or "")
        try:
            self.detail_view.clear()
        except tk.TclError:  # pragma: no cover
            pass
        if record is None:
            try:
                self.detail_title.configure(text="No library selected")
                for widget in (self.install_button, self.uninstall_button, self.include_button):
                    widget.configure(state="disabled")
            except tk.TclError:  # pragma: no cover
                pass
            return
        try:
            self.detail_title.configure(text=f"{record.display_name}  \u00b7  v{record.version or record.latest_version}")
            lines = [
                ("head", record.sentence or "No summary available"),
                ("value", ""),
                ("key", "Installed"), ("value", record.version or "not installed"),
                ("key", "Latest"), ("value", record.latest_version or "unknown"),
                ("key", "Author"), ("value", record.author or "-"),
                ("key", "Category"), ("value", record.category or "-"),
                ("key", "License"), ("value", record.license or "-"),
                ("key", "Architectures"), ("value", record.architectures or "any"),
                ("key", "Scope"), ("value", record.scope or "-"),
                ("key", "Includes"), ("value", ", ".join(record.provides_includes[:12]) or "-"),
            ]
            if record.depends:
                lines += [("key", "Depends"), ("value", record.depends)]
            if record.paragraph:
                lines += [("value", ""), ("value", record.paragraph[:900])]
            for url in (record.website, record.repository_url):
                if url:
                    lines.append(("url", url))
            for tag, text in lines:
                self.detail_view.append_line(text, (tag,))
            versions = list(record.available_versions) or ([record.latest_version] if record.latest_version else [])
            self.version_menu.configure(values=versions or ["latest"])
            self.version_menu.set(versions[0] if versions else "latest")
            for widget in (self.install_button, self.uninstall_button, self.include_button):
                widget.configure(state="normal")
            self.install_button.configure(text="Update" if record.update_available else
                                          ("Reinstall" if record.installed else "Install"))
            self.uninstall_button.configure(state="normal" if record.installed else "disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def _selected_record(self) -> Optional[LibraryRecord]:
        key = self.table.selected_key()
        return self._records.get(key) if key else None

    # ----------------------------------------------------------------- actions
    def install_selected(self) -> None:
        """Install (or update) the selected library, honouring the version picker."""
        record = self._selected_record()
        if record is None:
            self._status("select a library first")
            return
        version = ""
        try:
            chosen = str(self.version_menu.get())
            version = "" if chosen in ("", "latest") else chosen
        except tk.TclError:  # pragma: no cover
            pass
        spec = record.name if not version else f"{record.name}@{version}"
        project_local = False
        try:
            project_local = bool(self.project_scope.get()) and self._project() is not None
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        self._install_spec(spec, project_local=project_local, record=record)

    def _install_spec(self, spec: str, *, project_local: bool = False,
                      record: Optional[LibraryRecord] = None,
                      zip_path: str = "", git_url: str = "", branch: str = "") -> None:
        dependencies = self._missing_dependencies(record)
        specs = [spec] + [dep for dep in dependencies if dep != spec]
        confirm = True
        if record is not None and record.depends:
            confirm = ask_yes_no(
                self,
                f"Install {spec}{' and its dependencies (' + ', '.join(dependencies) + ')' if dependencies else ''}?",
                detail=f"arduino-cli lib install {' '.join(specs)}",
                yes_label="Install", palette=self.palette,
            )
        if not confirm:
            return

        def work(context: Any) -> Any:
            if zip_path:
                return self.manager.install_zip(zip_path, project=self._project(),
                                                project_local=project_local, on_line=context.log,
                                                context=context)
            if git_url:
                return self.manager.install_git(git_url, project=self._project(),
                                                project_local=project_local, branch=branch,
                                                on_line=context.log, context=context)
            if len(specs) > 1:
                return self.manager.install_many(specs, on_line=context.log, context=context)
            return self.manager.install(specs[0], on_line=context.log, context=context,
                                        project=self._project(), project_local=project_local)

        def done(result: TaskResult) -> None:
            if not result.ok:
                self._show_error("Installation failed", result)
                return
            self._status(f"installed {spec}" + (" (project)" if project_local else ""))
            self._toast("Library installed", f"{spec} is ready to use.\n"
                       "Add it with the 'Add #include' button or type the include yourself.")
            if self._on_project_changed is not None:
                self._on_project_changed()
            self.refresh_installed()

        label = "install" if not zip_path else "install from ZIP"
        self._submit(f"lib {label} {spec}", work, lane=LANE_BUILD, on_done=done,
                     busy_label=f"{label} {spec}\u2026")

    def _missing_dependencies(self, record: Optional[LibraryRecord]) -> list[str]:
        """Names of declared dependencies that are not installed yet."""
        if record is None or not record.depends:
            return []
        wanted: list[str] = []
        for chunk in record.depends.replace(";", ",").split(","):
            name = chunk.split("(")[0].strip()
            if not name:
                continue
            try:
                found = self.manager.find_installed(name)
            except Exception:  # pragma: no cover
                found = None
            if found is None:
                wanted.append(name)
        return wanted

    def uninstall_selected(self) -> None:
        record = self._selected_record()
        if record is None:
            self._status("select a library first")
            return
        project = self._project()
        if record.location.upper() in ("PROJECT", "SKETCHBOOK") and project is not None:
            if not ask_yes_no(self, f"Remove {record.name} from this project?",
                              detail="The folder under libraries/ will be deleted.",
                              yes_label="Remove", kind="warning", palette=self.palette):
                return

            def remove(context: Any) -> bool:
                return self.manager.remove_project_library(project, record)

            def done_remove(result: TaskResult) -> None:
                self._status("removed" if result.payload else "nothing removed")
                self.refresh_project()

            self._submit("lib remove project", remove, lane=LANE_BUILD, on_done=done_remove)
            return
        if not ask_yes_no(self, f"Uninstall {record.name}?",
                          detail="Other sketches that use it will stop compiling until it is installed again.",
                          yes_label="Uninstall", kind="warning", palette=self.palette):
            return

        def work(context: Any) -> Any:
            return self.manager.uninstall(record.name, on_line=context.log, context=context)

        def done(result: TaskResult) -> None:
            if not result.ok:
                self._show_error("Uninstall failed", result)
                return
            self._status(f"uninstalled {record.name}")
            self.refresh_installed()

        self._submit(f"lib uninstall {record.name}", work, lane=LANE_BUILD, on_done=done,
                     busy_label=f"uninstalling {record.name}\u2026")

    def add_include_selected(self) -> None:
        """Insert ``#include <Header.h>`` for the selected library."""
        record = self._selected_record()
        if record is None:
            return
        headers = list(record.provides_includes) or [f"{record.name}.h"]
        header = headers[0]
        if len(headers) > 1:
            from .widgets.dialogs import ask_choice

            chosen = ask_choice(self, headers, title="Choose a header",
                                label=f"{record.name} ships more than one header:")
            header = chosen or header
        if self._on_add_include is not None:
            self._on_add_include(header)
            self._status(f"added #include <{header}>")
        else:
            self._status(f"this library provides <{header}>")

    def update_index(self) -> None:
        """``arduino-cli core update-index`` + ``lib update-index``."""
        if not ask_yes_no(self, "Update the Arduino package index?",
                          detail="Downloads the index of boards and libraries. This needs internet access and "
                                 "can take a minute.", yes_label="Update", palette=self.palette):
            return

        def work(context: Any) -> Any:
            context.progress(None, "updating index")
            result = self.manager.update_index(on_line=context.log, context=context)
            context.progress(1.0, "index updated")
            return result

        def done(result: TaskResult) -> None:
            ok = bool(result.ok)
            self._status("index updated" if ok else "index update failed")
            if not ok:
                self._show_error("Could not update the index", result)
            else:
                self._run_search()

        self._submit("lib update-index", work, lane=LANE_BUILD, on_done=done,
                     busy_label="updating library index\u2026")

    def update_all_libraries(self) -> None:
        """Update every library that has a newer version in the index."""
        def pre(context: Any) -> list[str]:
            return [record.name for record in self.manager.updatable(fqbn=self._fqbn())]

        def done_pre(result: TaskResult) -> None:
            names = result.payload or []
            if not names:
                self._status("every library is up to date")
                return
            if not ask_plan(self, heading=f"Update {len(names)} librar{'y' if len(names) == 1 else 'ies'}?",
                            sections=[self._plan_section(names)],
                            accept_label="Update", require_ack=False, palette=self.palette):
                return

            def work(context: Any) -> Any:
                return self.manager.upgrade_named(names, on_line=context.log, context=context)

            def done(result: TaskResult) -> None:
                if not result.ok:
                    self._show_error("Update failed", result)
                    return
                self._status(f"updated {len(names)} libraries")
                self.refresh_installed()

            self._submit("lib update all", work, lane=LANE_BUILD, on_done=done, busy_label="updating libraries\u2026")

        self._submit("lib list --updatable", pre, on_done=done_pre, busy_label="checking for updates\u2026")

    @staticmethod
    def _plan_section(names: list[str]) -> Any:
        from .widgets.dialogs import PlanSection

        return PlanSection("Libraries", "\n".join(f"  {name}" for name in names[:40]), "mono")

    def install_from_zip(self) -> None:
        """``Add .ZIP library`` - installs a local archive."""
        from tkinter import filedialog

        try:
            path = filedialog.askopenfilename(
                parent=self, title="Choose a library ZIP", filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
            )
        except tk.TclError:  # pragma: no cover
            path = ""
        if not path:
            return
        if not ask_plan(self, heading=f"Install library from archive?\n\n{Path(path).name}",
                        sections=[_zip_plan_section(path, self._project(), bool(self.project_scope.get()))],
                        accept_label="Install", require_ack=False, palette=self.palette):
            return
        project_local = bool(self.project_scope.get())
        self._install_spec(Path(path).stem, project_local=project_local, zip_path=path)

    def install_from_git(self) -> None:
        """``Add library from Git URL`` (``--git-url``), optional branch."""
        url = ask_text(self, "Git URL of the library repository",
                       initial="https://github.com/adafruit/Adafruit_BME280_Library.git",
                       title="Install from Git", palette=self.palette)
        if not url:
            return
        url = url.strip()
        if not url.lower().startswith(("http://", "https://", "git://", "git@")):
            ask_message(self, kind="error", title="Not a Git URL",
                        message="Use an https:// (or git://) repository URL.", palette=self.palette)
            return
        branch = ""
        if ask_yes_no(self, "Install from a specific branch or tag?",
                      detail="Leave empty for the default branch.", yes_label="Yes, choose a ref",
                      no_label="No", palette=self.palette):
            branch = ask_text(self, "Branch or tag", initial="master", title="Git reference",
                              palette=self.palette) or ""
        project_local = bool(self.project_scope.get())
        self._install_spec(url, project_local=project_local, git_url=url, branch=branch.strip())

    # ------------------------------------------------- missing include support
    def suggest_install(self, headers: list[str], *, auto_include: bool = True) -> bool:
        """Ask the user to install libraries for unresolved includes.

        Called by the app when a compile fails with ``fatal error: X.h: No such
        file or directory``.  Returns ``True`` when an install was started.
        """
        wanted = [header for header in headers if header]
        if not wanted:
            return False

        def work(context: Any) -> dict[str, list[LibraryRecord]]:
            suggestions: dict[str, list[LibraryRecord]] = {}
            for header in wanted:
                # prefer an exact provider from the index, then fall back to a search
                resolved = self.manager.resolve_includes([header])
                record = resolved.get(header)
                if record is not None and not record.installed:
                    suggestions[header] = [record]
                    continue
                stem = Path(header).stem
                candidates = [item for item in self.manager.search(stem)
                              if stem.lower() in item.searchable().lower()][:6]
                suggestions[header] = candidates

        def done(result: TaskResult) -> None:
            if not result.ok or not result.payload:
                ask_message(self, kind="warning", title="No library found",
                            message="No library in the index provides: " + ", ".join(wanted),
                            detail="Try 'Update index', or install the library from a ZIP / Git URL.",
                            palette=self.palette)
                return
            payload: dict[str, list[LibraryRecord]] = result.payload or {}
            choices: list[tuple[str, LibraryRecord]] = []
            for header, records in payload.items():
                for record in records[:3]:
                    choices.append((header, record))
            if not choices:
                ask_message(self, kind="warning", title="No library found",
                            message="Nothing in the index provides " + ", ".join(wanted),
                            detail="Search the Library Manager manually, or install from ZIP / Git.",
                            palette=self.palette)
                return
            header, record = choices[0]
            extra = "\n".join(f"  {name}: {rec.name}" for name, rec in choices[1:6])
            if not ask_plan(self, heading=f"<{header}> was not found - install a library that provides it?",
                            sections=[_include_plan_section(header, record, extra)],
                            accept_label="Install", require_ack=False, palette=self.palette):
                return
            self._install_spec(record.name, record=record)
            if auto_include and self._on_add_include is not None and record.provides_includes:
                self._on_add_include(record.provides_includes[0])
            return True

        self._submit("suggest libraries for includes", work, on_done=done,
                     busy_label="searching for the missing library\u2026")
        return True

    # ------------------------------------------------------------------- misc
    def _activate_row(self, key: str) -> None:
        record = self._records.get(key)
        if record is None:
            return
        if record.installed:
            self._show_record(key)
        else:
            self.install_selected()

    def _row_menu(self, key: str, x: int, y: int) -> None:
        record = self._records.get(key) if key else None
        menu = tk.Menu(self, tearoff=0)
        if record is not None:
            menu.add_command(label=("Install " if not record.installed else "Reinstall ") + record.name,
                             command=self.install_selected)
            menu.add_command(label="Uninstall", command=self.uninstall_selected,
                             state="normal" if record.installed else "disabled")
            menu.add_command(label="Add #include", command=self.add_include_selected,
                             state="normal" if record.provides_includes else "disabled")
            menu.add_separator()
            menu.add_command(label="Copy repository URL",
                             command=lambda: self._copy(record.repository_url or record.website or ""))
            menu.add_command(label="Copy library name", command=lambda: self._copy(record.name))
            menu.add_separator()
        menu.add_command(label="Refresh list", command=self.refresh)
        menu.add_command(label="Update index", command=self.update_index)
        try:
            menu.tk_popup(x, y)
        finally:  # pragma: no cover
            menu.grab_release()

    def _copy(self, value: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
            self._status("copied")
        except tk.TclError:  # pragma: no cover
            pass

    def _show_error(self, title: str, result: TaskResult) -> None:
        """Explain a failed library task (``TaskResult.error`` is an exception)."""
        detail = str(result.error).strip() if result.error is not None else ""
        if not detail:
            detail = "See the console output for details."
        lowered = detail.lower()
        hint = ""
        if "not found" in lowered or "is not a recognized" in lowered:
            hint = "\n\narduino-cli could not run the command - check the CLI path in Settings."
        elif "internet" in lowered or "network" in lowered or "timeout" in lowered:
            hint = "\n\nThe Library Manager needs an internet connection to download libraries."
        MessageDialog(self, kind="error", title=title, message=detail[:900] + hint,
                      palette=self.palette, width=620).run()
        self._status(f"{title.lower()}: {detail.splitlines()[0][:90]}")

    def _toast(self, title: str, message: str) -> None:
        """Non blocking success notice (the console keeps the full log)."""
        try:
            self.status_label.configure(text=f"{title}: {message.splitlines()[0][:80]}")
        except tk.TclError:  # pragma: no cover
            pass

    def _project(self) -> Any:
        if self._get_project is None:
            return None
        try:
            return self._get_project()
        except Exception:  # pragma: no cover
            return None

    def _fqbn(self) -> str:
        if self._get_fqbn is None:
            return ""
        try:
            return self._get_fqbn() or ""
        except Exception:  # pragma: no cover
            return ""

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
            self.search_entry.configure(fg_color=palette.surface, text_color=palette.text,
                                        border_color=palette.border)
            self.status_label.configure(text_color=palette.text_dim)
            self.count_label.configure(text_color=palette.text_muted)
            self.detail_title.configure(text_color=palette.text)
        except tk.TclError:  # pragma: no cover
            pass
        self.table.refresh_palette(palette)

    def destroy(self) -> None:
        """Cancel pending debounced searches."""
        try:
            self._debouncer.cancel()
        except Exception:  # pragma: no cover
            pass
        super().destroy()


def _zip_plan_section(path: str, project: Any, project_local: bool) -> Any:
    from .widgets.dialogs import PlanSection

    target = str(Path(project.root) / "libraries") if (project_local and project is not None) else "the data folder"
    return PlanSection(
        "Archive",
        f"  File     : {path}\n  Installs : {target}\n\n"
        "The archive is unpacked and the library is ready to use immediately.",
        "info",
    )


def _include_plan_section(header: str, record: LibraryRecord, extra: str) -> Any:
    from .widgets.dialogs import PlanSection

    body = (f"  Header     : <{header}>\n  Library    : {record.name}\n"
            f"  Version    : {record.latest_version or record.version or 'latest'}\n"
            f"  Author     : {record.author or '-'}\n")
    if extra:
        body += f"\nOther candidates:\n{extra}\n"
    return PlanSection("Will install", body, "mono")
