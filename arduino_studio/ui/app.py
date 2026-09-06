"""The main window: wires every panel, service and menu together.

Layout (as required by the spec):

``toolbar``  New / Open / Save / Verify / Upload / Board / Port / Serial / Libraries / Settings
``left``     project explorer
``center``   editor tabs, or the Libraries / Bootloader / Settings screen
``bottom``   Console | Serial Monitor | Terminal tabs
``status``   project · board · port · current operation · elapsed time

Rules the code follows:

* nothing slow runs on the UI thread - ``arduino-cli`` and file work go through
  :class:`~arduino_studio.core.runner.TaskRunner`, and every callback that
  touches a widget is re-posted with ``after()`` by the runner;
* the Serial Monitor releases the COM port before an upload and offers to
  re-open it afterwards;
* an upload without a port, or any command without ``arduino-cli``, is refused
  with an actionable message instead of a stack trace;
* dialogs are only used for confirmations that matter (unsaved files, fuse
  burning, destructive terminal commands, missing libraries).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.arduino_cli import (
    CLIError,
    CompileReport,
    Diagnostic,
    PortInfo,
)
from ..core.boards import (
    BUILTIN_BOARDS,
    CORE_INSTALL_HINTS,
    baud_for_fqbn,
    board_for_fqbn,
    core_for_fqbn,
    core_install_name,
    display_name_for_fqbn,
    sketch_memory_report,
    sort_board_labels,
)
from ..core.bootloader import BootloaderService
from ..core.examples import EXAMPLES, example_menu_label, example_project_name, get_example, render_files
from ..core.library_manager import LibraryManager
from ..core.process import CommandError
from ..core.project import Project, ProjectError, ProjectManager
from ..core.runner import LANE_BUILD, LANE_QUERY, TaskResult, TaskRunner
from ..core.serial_service import list_serial_ports
from ..core.settings import Settings, SettingsStore
from ..core.utils import (
    get_logger,
    human_bytes,
    human_duration,
    open_path_in_file_manager,
    setup_logging,
)
from .bootloader_panel import BootloaderPanel
from .editor_tabs import EditorTabs
from .libraries_panel import LibrariesPanel
from .serial_monitor import SerialMonitorPanel
from .settings_view import SettingsView
from .setup_wizard import run_first_run_wizard
from .terminal_panel import TerminalPanel
from .theme import Palette, apply_tk_theme, palette_for
from .widgets.console import ConsolePanel
from .widgets.dialogs import ask_message, ask_text, ask_yes_no
from .widgets.explorer import ProjectExplorer
from .widgets.project_dialog import ask_new_project
from .widgets.status_bar import StatusBar
from .widgets.toolbar import Toolbar

__all__ = ["ArduinoStudioApp", "create_app"]

SIDEBAR_MIN_WIDTH = 150
SIDEBAR_DEFAULT_WIDTH = 268
BOTTOM_DEFAULT_HEIGHT = 250


@dataclass
class BuildRequest:
    """Everything needed to run one verify/upload operation."""

    sketch_dir: Path
    fqbn: str
    build_dir: Optional[Path]
    project: Optional[Project]
    libraries: tuple[Path, ...] = ()
    warnings: str = "default"
    clean: bool = False
    verbose: bool = False
    upload: bool = False
    port: str = ""
    programmer: str = ""
    verify_upload: bool = False
    no_reset: bool = False


class ArduinoStudioApp(ctk.CTk):
    """Top level window of Arduino Studio."""

    def __init__(
        self,
        *,
        config_dir: Optional[Path] = None,
        open_path: Optional[Path] = None,
        force_setup: bool = False,
        skip_setup: bool = False,
        log_level: str = "INFO",
    ) -> None:
        super().__init__(fg_color="#101418")
        self._app_start = time.monotonic()
        self._last_port_scan = 0.0
        self._pending_open = Path(open_path).expanduser() if open_path else None
        self._force_setup = bool(force_setup)
        self._skip_setup = bool(skip_setup)

        # ------------------------------------------------------------- config
        self.store = SettingsStore(config_dir)
        self.settings: Settings = self.store.load()
        setup_logging(log_dir=self.store.log_dir, level=_log_level(log_level))
        self._log = get_logger("app")
        self._log.info("starting Arduino Studio %s (config: %s)", _version_string(), self.store.path)
        self.store.add_listener(self._on_setting_changed)
        self._cli_probe_failed = False

        self.palette: Palette = self._palette_from_settings()
        self._configure_window()
        apply_tk_theme(self, self.palette)

        # ------------------------------------------------------------ services
        self.runner = TaskRunner(ui_post=self._post)
        self.cli = self._new_cli()
        self.projects = ProjectManager(default_parent=self._sketchbook_parent())
        self.libraries = LibraryManager(self.cli, data_dir=self.store.data_dir)
        self.bootloader = BootloaderService(self.cli, cli_path=self.settings.effective_cli_path(),
                                           data_dir=self.store.data_dir)

        self.project: Optional[Project] = None
        self._board_labels: list[str] = []
        self._board_map: dict[str, str] = {}       # label -> fqbn
        self._port_labels: list[str] = []
        self._port_map: dict[str, str] = {}        # label -> address
        self._last_build_dir: Optional[Path] = None
        self._last_report: Optional[CompileReport] = None
        self._external_warnings = 0
        self._closing = False
        self._serial_was_open = False
        self._current_view = "editor"
        self._current_bottom = "console"
        self._sidebar_width = SIDEBAR_DEFAULT_WIDTH
        self._sidebar_visible = True
        self._bottom_collapsed = False
        self._timers: list[str] = []

        # ----------------------------------------------------------------- UI
        self._build_ui()
        self._build_menus()
        self._bind_shortcuts()
        self._apply_theme_to_children()
        self._show_view("editor", focus=False)
        self._show_bottom("console")

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.bind("<FocusIn>", self._on_window_focus, add=True)
        self.after(60, lambda: self._startup(force_setup=self._force_setup, skip_setup=self._skip_setup))

    # ================================================================ startup
    def _configure_window(self) -> None:
        self.title("Arduino Studio")
        settings = self.settings
        width = max(900, int(settings.window_width or 1320))
        height = max(600, int(settings.window_height or 820))
        x, y = int(settings.window_x or -1), int(settings.window_y or -1)
        geometry = f"{width}x{height}" + (f"+{x}+{y}" if x >= 0 and y >= 0 else "")
        try:
            self.geometry(geometry)
            self.minsize(980, 620)
            if settings.window_maximized:
                self.state("zoomed")
        except tk.TclError:  # pragma: no cover - window manager quirks
            pass
        self._apply_window_icon()
        for setter, value in ((getattr(ctk, "set_appearance_mode", None), _appearance_for_ctk(settings.appearance_mode)),
                              (getattr(ctk, "set_default_color_theme", None), "blue")):
            if callable(setter):
                try:
                    setter(value)  # type: ignore[misc]
                except Exception:  # pragma: no cover - cosmetic only
                    self._log.debug("ctk global theme %r not applied", value)

    def _palette_from_settings(self) -> Palette:
        return palette_for(self.settings.appearance_mode, self.settings.accent_color or None)

    def _apply_window_icon(self) -> None:
        """Use ``arduino_studio/resources/arduino.ico`` when it is available.

        The icon is generated by ``python tools/make_icon.py`` (and embedded in
        the PyInstaller build), so a plain source checkout without it simply
        keeps the default Tk icon instead of raising.
        """
        candidates = []
        bundle = getattr(sys, "_MEIPASS", "")
        if bundle:
            candidates.append(Path(bundle) / "arduino.ico")
        here = Path(__file__).resolve().parent.parent
        candidates.append(here / "resources" / "arduino.ico")
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                self.iconbitmap(default=str(candidate))
                return
            except tk.TclError:  # pragma: no cover - no icon support / X11
                continue
        try:
            self.iconbitmap(default="")  # keep Tk from loading a stock icon
        except tk.TclError:  # pragma: no cover
            pass

    def _startup(self, *, force_setup: bool = False, skip_setup: bool = False) -> None:
        """Run after the window exists: wizard, CLI probe, session restore."""
        needs_setup = force_setup or (not self.settings.first_run_completed)
        if needs_setup and not skip_setup:
            self._run_setup_wizard(first_run=not force_setup)
        self.refresh_boards(refresh_ports=True)
        self._probe_cli()
        self._restore_session()
        self._start_timers()
        self.status.finish_operation(True, f"ready in {human_duration(time.monotonic() - self._app_start)}")

    def _run_setup_wizard(self, *, first_run: bool = True) -> bool:
        """Show the first-run wizard; returns whether it completed."""
        done = run_first_run_wizard(self, self.palette, store=self.store, settings=self.settings,
                                     runner=self.runner, on_board_picked=self._apply_board_choice)
        if done:
            self._rebuild_services()
            if first_run:
                self.log("Setup complete. You can change anything later in Settings.", "ok")
            self.refresh_boards(refresh_ports=True)
            if not self.project:
                self._offer_starter_project()
        self.status.set_message("setup finished" if done else "setup skipped")
        return bool(done)

    def _offer_starter_project(self) -> None:
        """Create the sample sketch the wizard asked for (or offer to)."""
        example = get_example("eeprom-string")
        if example is None:
            return
        try:
            self._create_example_project(example)
        except Exception as exc:  # pragma: no cover - the user can always do it by hand
            self._log.exception("starter project failed")
            self.status.set_error(f"could not create the starter sketch: {exc}")

    def _probe_cli(self) -> None:
        """Ask ``arduino-cli version`` in the background and report the result."""

        def work(context: Any) -> Any:
            return self.cli.probe(self.settings.effective_cli_path())

        def done(result: TaskResult) -> None:
            info = result.payload
            if info is None:
                return
            self._cli_probe_failed = not bool(getattr(info, "ok", False))
            if getattr(result, "ok", False) and getattr(info, "ok", False):
                self.status.set_message(f"arduino-cli {info.version} ready")
                self.log(f"arduino-cli {info.version} ({info.path})", "ok")
                self.settings_view.set_cli_status(f"arduino-cli {info.version} found at {info.path}", True)
                self.libraries_panel.refresh_palette(self.palette)
                return
            message = getattr(info, "message", None) or "arduino-cli did not answer"
            self.log(f"arduino-cli problem: {message}", "error")
            self.settings_view.set_cli_status(message, False)
            if self._settings_prompt_for_cli(message):
                return
            self.status.set_error("arduino-cli not available")

        try:
            self.runner.submit("app:cli-probe", work, lane=LANE_QUERY, on_done=done)
        except Exception as exc:  # pragma: no cover - runner must not explode
            self._log.exception("cli probe could not start")
            self.status.set_error(f"could not check arduino-cli: {exc}")

    def _settings_prompt_for_cli(self, message: str) -> bool:
        """Offer the setup screen when the CLI is missing; True when it was opened."""
        answer = ask_message(
            self, kind="warning", title="arduino-cli not usable",
            message=message,
            detail="Verify and Upload need a working arduino-cli. The Arduino IDE does not have to be open, "
                   "but the CLI must be installed (or its path given in Settings).",
            buttons=("Continue anyway", "Open setup", "Run wizard"), palette=self.palette, width=620,
        )
        if answer == "Open setup":
            self.open_settings("Arduino CLI")
            return True
        if answer == "Run wizard":
            self._run_setup_wizard(first_run=False)
            return True
        return False

    def _restore_session(self) -> None:
        """Reopen the last project and its editor tabs."""
        path: Optional[Path] = None
        if self._pending_open is not None:
            path = self._pending_open
            self._pending_open = None
        elif str(self.settings.active_project or "").strip():
            candidate = Path(str(self.settings.active_project)).expanduser()
            path = candidate if candidate.is_dir() else None
        if path is not None:
            self.open_project(path, quiet=True)
            files = [Path(item).expanduser() for item in (self.settings.open_files or [])]
            for item in files:
                if item.is_file():
                    self.tabs.open_file(item, activate=False)
            if self.settings.open_files:
                self.status.set_message(f"restored {len(self.settings.open_files)} open file(s)")
        else:
            self.explorer.set_empty_state(True)
            self.status.set_message("no project open - File ▸ New Project to start")

    def _start_timers(self) -> None:
        """Periodic housekeeping: autosave and stale-external-file checks."""
        interval = int(self.settings.autosave_interval_sec or 0)
        if interval > 0:
            self._timers.append(self.after(max(2000, interval * 1000), self._autosave_tick))
        self._timers.append(self.after(30_000, self._external_tick))

    def _restart_timers(self) -> None:
        for ident in self._timers:
            try:
                self.after_cancel(ident)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        self._timers.clear()
        self._start_timers()

    def _autosave_tick(self) -> None:
        saved = 0
        try:
            saved = self.tabs.autosave_dirty()
        except Exception:  # pragma: no cover
            self._log.exception("autosave failed")
        if saved:
            self.log(f"auto-saved {saved} file(s)", "dim")
        interval = max(2000, int(self.settings.autosave_interval_sec or 0) * 1000)
        self._timers.append(self.after(interval, self._autosave_tick))

    def _external_tick(self) -> None:
        changed: list[Path] = []
        try:
            changed = self.tabs.check_external_changes()
        except Exception:  # pragma: no cover
            self._log.debug("external change check failed", exc_info=True)
        if changed:
            self._external_warnings += 1
            self.status.set_message(f"{len(changed)} file(s) changed on disk - reload with Sketch ▸ Reload from disk")
        self._timers.append(self.after(30_000, self._external_tick))

    def _on_window_focus(self, event: Any) -> None:
        """Refresh the port list when the window is (re-)focused."""
        if getattr(event, "widget", None) is not self:
            return
        if time.monotonic() - getattr(self, "_last_port_scan", 0.0) < 4.0:
            return
        self._last_port_scan = time.monotonic()
        try:
            self.refresh_ports(quiet=True)
        except Exception:  # pragma: no cover
            pass

    # ==================================================================== UI
    def _build_ui(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.toolbar = Toolbar(
            self, palette,
            on_new_project=self.new_project, on_open_project=self.open_project_dialog, on_save=self.save,
            on_verify=self.verify, on_upload=self.upload, on_board_selected=self._on_board_menu_pick,
            on_port_selected=self._on_port_menu_pick, on_serial=self.toggle_serial,
            on_libraries=self.open_libraries, on_bootloader=self.open_bootloader, on_settings=self.open_settings,
        )
        self.toolbar.grid(row=0, column=0, sticky="ew")
        self.toolbar.set_refresh_handler(self.refresh_all)

        self.main_row = ctk.CTkFrame(self, fg_color=palette.window_bg, corner_radius=0)
        self.main_row.grid(row=1, column=0, sticky="nsew")
        self.main_row.grid_rowconfigure(0, weight=1)
        self.main_row.grid_columnconfigure(1, weight=1)

        self.explorer = ProjectExplorer(
            self.main_row, palette,
            on_open_file=self._open_from_explorer, on_new_file=self.new_file, on_new_folder=self.new_folder,
            on_rename=self._rename_from_explorer, on_delete=self._delete_from_explorer,
            on_duplicate=self._duplicate_from_explorer, on_import_file=self.import_file,
            on_refresh=self.refresh_explorer, on_project_action=self._project_action,
            on_status=lambda message: self.status.set_message(message),
        )
        self.explorer.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        self.explorer.grid_propagate(False)
        try:
            self.explorer.configure(width=SIDEBAR_DEFAULT_WIDTH)
        except tk.TclError:  # pragma: no cover
            pass

        self.center = ctk.CTkFrame(self.main_row, fg_color=palette.window_bg, corner_radius=0)
        self.center.grid(row=0, column=1, sticky="nsew", padx=(6, 8), pady=8)
        self.center.grid_rowconfigure(0, weight=1)
        self.center.grid_columnconfigure(0, weight=1)

        # ------------------------------------------------------------- bottom
        bottom = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=0)
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.grid_rowconfigure(1, weight=1)
        bottom.grid_columnconfigure(0, weight=1)
        self._bottom = bottom
        self.grid_rowconfigure(2, weight=0)

        bar = ctk.CTkFrame(bottom, fg_color=palette.panel_bg, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(4, weight=1)
        self.bottom_tabs = ctk.CTkSegmentedButton(
            bar, values=["Console", "Serial Monitor", "Terminal"], height=28, width=340,
            fg_color=palette.surface, selected_color=palette.accent, selected_hover_color=palette.accent_hover,
            unselected_color=palette.surface, unselected_hover_color=palette.hover,
            text_color=palette.text, font=(palette.font_family, 10), command=self._on_bottom_segment,
        )
        self.bottom_tabs.set("Console")
        self.bottom_tabs.grid(row=0, column=0, sticky="w", padx=(8, 6), pady=(6, 4))
        ctk.CTkButton(bar, text="Clear", width=62, height=26, corner_radius=6, fg_color="transparent",
                      border_width=1, border_color=palette.border, hover_color=palette.hover,
                      text_color=palette.text_dim, command=self._clear_bottom).grid(row=0, column=1, padx=(0, 4), pady=6)
        ctk.CTkButton(bar, text="Collapse", width=76, height=26, corner_radius=6, fg_color="transparent",
                      border_width=1, border_color=palette.border, hover_color=palette.hover,
                      text_color=palette.text_dim, command=self.toggle_bottom).grid(row=0, column=2, padx=(0, 4), pady=6)
        ctk.CTkButton(bar, text="Cancel task", width=88, height=26, corner_radius=6, fg_color="transparent",
                      border_width=1, border_color=palette.border, hover_color=palette.hover,
                      text_color=palette.error, command=self._cancel_current).grid(row=0, column=3, pady=6)
        self.bottom_info = ctk.CTkLabel(bar, text="", font=(palette.font_family, 10), text_color=palette.text_muted,
                                        anchor="e")
        self.bottom_info.grid(row=0, column=4, sticky="e", padx=(6, 10), pady=6)

        self.bottom_body = ctk.CTkFrame(bottom, fg_color=palette.window_bg, corner_radius=0)
        self.bottom_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.bottom_body.grid_rowconfigure(0, weight=1)
        self.bottom_body.grid_columnconfigure(0, weight=1)

        self.status = StatusBar(self, palette, on_status_click=self._on_status_click)
        self.status.grid(row=3, column=0, sticky="ew")
        self.status.set_project("no project")
        self.status.set_board(display_name_for_fqbn(self.fqbn()) or "no board")
        self.status.set_port(self.settings.port or "", connected=False)

        self.console = ConsolePanel(self.bottom_body, palette, on_jump=self._jump_to,
                                    on_cancel=self._cancel_current, on_save_log=self._save_console_log,
                                    settings=self.settings)
        self.serial_panel = SerialMonitorPanel(
            self.bottom_body, palette, settings=self.settings, ui_post=self._post,
            on_status=self.status.set_message, on_open_state=self._on_serial_state,
            on_request_ports=lambda: self.refresh_ports(),
        )
        self.terminal_panel = TerminalPanel(
            self.bottom_body, palette, settings=self.settings, ui_post=self._post,
            on_status=self.status.set_message, on_files_changed=self.refresh_explorer,
            cli_path_provider=lambda: self.settings.effective_cli_path(),
            project_dir_provider=lambda: self.project.root if self.project is not None else None,
        )
        self.bottom_panels = {"console": self.console, "serial": self.serial_panel, "terminal": self.terminal_panel}


        self.tabs = EditorTabs(
            self.center, palette, settings=self.settings, on_project_changed=self._on_project_changed,
            on_status=self.status.set_message, on_active_changed=self._on_active_file,
            on_new_file=self.new_file_inline,
        )
        self.tabs.set_unsaved_handler(self._unsaved_handler)

        self.libraries_panel = LibrariesPanel(
            self.center, palette, manager=self.libraries, runner=self.runner, console=self.console,
            settings=self.settings,
            get_project=lambda: self.project, get_fqbn=self.fqbn, on_status=self.status.set_message,
            on_add_include=self._insert_include, on_project_changed=self._on_project_changed,
        )
        self.bootloader_panel = BootloaderPanel(
            self.center, palette, service=self.bootloader, runner=self.runner, console=self.console,
            settings=self.settings, get_fqbn=self.fqbn, get_port=self.port, get_ports=lambda: self._port_labels,
            get_project=lambda: self.project, get_build_dir=lambda: self._last_build_dir,
            on_status=self.status.set_message, on_fqbn_changed=self._apply_board_choice,
        )
        self.settings_view = SettingsView(
            self.center, palette, store=self.store, settings=self.settings, on_change=self._on_setting_changed,
            on_detect_cli=self.detect_cli, on_test_cli=self._probe_cli, on_close=lambda: self._show_view("editor"),
        )
        self.views: dict[str, Any] = {
            "editor": self.tabs, "libraries": self.libraries_panel,
            "bootloader": self.bootloader_panel, "settings": self.settings_view,
        }


    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        palette = self.palette
        menubar = tk.Menu(self)
        self._menubar = menubar
        try:
            self.configure(menu=menubar)
        except tk.TclError:  # pragma: no cover
            pass

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New Project…", command=self.new_project, accelerator="Ctrl+Shift+N")
        examples = tk.Menu(file_menu, tearoff=0)
        for example in EXAMPLES:
            examples.add_command(label=example_menu_label(example),
                                 command=lambda item=example: self.new_example(item.id))
        file_menu.add_cascade(label="New Example", menu=examples)
        file_menu.add_command(label="Open Project…", command=self.open_project_dialog, accelerator="Ctrl+O")
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        self._rebuild_recent_menu()
        file_menu.add_separator()
        file_menu.add_command(label="Save", command=self.save, accelerator="Ctrl+S")
        file_menu.add_command(label="Save All", command=self.save_all, accelerator="Ctrl+Shift+S")
        file_menu.add_command(label="Close Project", command=self.close_project)
        file_menu.add_command(label="Close Tab", command=self.close_tab, accelerator="Ctrl+W")
        file_menu.add_separator()
        file_menu.add_command(label="Rename Project…", command=lambda: self._project_action("rename"))
        file_menu.add_command(label="Duplicate Project…", command=lambda: self._project_action("duplicate"))
        file_menu.add_command(label="Show Project Folder", command=lambda: self._project_action("reveal"))
        file_menu.add_command(label="Project Info…", command=lambda: self._project_action("info"))
        file_menu.add_separator()
        file_menu.add_command(label="Settings…", command=self.open_settings, accelerator="Ctrl+,")
        file_menu.add_command(label="Quit", command=self.quit_app, accelerator="Alt+F4")
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Undo", command=lambda: self._editor_action("undo"), accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=lambda: self._editor_action("redo"), accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Find…", command=self.find, accelerator="Ctrl+F")
        edit_menu.add_command(label="Find Next", command=lambda: self._editor_action("find_next"), accelerator="F3")
        edit_menu.add_command(label="Find Previous", command=lambda: self._editor_action("find_previous"),
                             accelerator="Shift+F3")
        edit_menu.add_command(label="Replace…", command=self.replace, accelerator="Ctrl+H")
        edit_menu.add_command(label="Find All in File", command=lambda: self._editor_action("highlight_all"))
        edit_menu.add_separator()
        edit_menu.add_command(label="Toggle Comment", command=lambda: self._editor_action("comment"),
                             accelerator="Ctrl+/")
        edit_menu.add_command(label="Duplicate Line", command=lambda: self._editor_action("duplicate"),
                             accelerator="Ctrl+D")
        edit_menu.add_command(label="Delete Line", command=lambda: self._editor_action("delete_line"),
                             accelerator="Ctrl+Shift+K")
        edit_menu.add_command(label="Indent", command=lambda: self._editor_action("indent"), accelerator="Tab")
        edit_menu.add_command(label="Outdent", command=lambda: self._editor_action("outdent"), accelerator="Shift+Tab")
        edit_menu.add_command(label="Go to Matching Bracket", command=lambda: self._editor_action("bracket"),
                             accelerator="Ctrl+[")
        edit_menu.add_command(label="Next Error", command=lambda: self._editor_action("next_error"),
                             accelerator="F2")
        edit_menu.add_command(label="Previous Error", command=lambda: self._editor_action("prev_error"),
                             accelerator="Shift+F2")
        edit_menu.add_command(label="Go to Line…", command=self.goto_line, accelerator="Ctrl+G")
        menubar.add_cascade(label="Edit", menu=edit_menu)

        sketch_menu = tk.Menu(menubar, tearoff=0)
        sketch_menu.add_command(label="Verify", command=self.verify, accelerator="Ctrl+R")
        sketch_menu.add_command(label="Verify (Clean Build)", command=self.verify_clean, accelerator="Ctrl+Shift+R")
        sketch_menu.add_command(label="Upload", command=self.upload, accelerator="Ctrl+U")
        sketch_menu.add_command(label="Upload Using Programmer", command=self.upload_programmer,
                                accelerator="Ctrl+Shift+U")
        sketch_menu.add_separator()
        sketch_menu.add_command(label="Export Compiled Binary", command=self.export_binaries)
        sketch_menu.add_command(label="Show Build Folder", command=lambda: self._project_action("build_folder"))
        sketch_menu.add_command(label="Clean Build Cache", command=self.clean_cache)
        sketch_menu.add_separator()
        sketch_menu.add_command(label="Reload Files from Disk", command=self.reload_from_disk)
        menubar.add_cascade(label="Sketch", menu=sketch_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        self.board_menu = tk.Menu(tools_menu, tearoff=0)
        tools_menu.add_cascade(label="Board", menu=self.board_menu)
        self.port_menu = tk.Menu(tools_menu, tearoff=0)
        tools_menu.add_cascade(label="Port", menu=self.port_menu)
        self.programmer_menu = tk.Menu(tools_menu, tearoff=0)
        tools_menu.add_cascade(label="Programmer", menu=self.programmer_menu)
        tools_menu.add_separator()
        tools_menu.add_command(label="Bootloader Manager…", command=self.open_bootloader,
                               accelerator="Ctrl+Shift+I")
        tools_menu.add_command(label="Burn Bootloader…", command=self.burn_bootloader)
        tools_menu.add_command(label="Read Chip Information", command=lambda: self._bootloader_action("read"))
        tools_menu.add_command(label="Backup Firmware…", command=lambda: self._bootloader_action("backup"))
        tools_menu.add_command(label="Flash ESP Binary…", command=lambda: self._bootloader_action("flash"))
        tools_menu.add_separator()
        tools_menu.add_command(label="Manage Libraries", command=self.open_libraries, accelerator="Ctrl+Shift+L")
        tools_menu.add_command(label="Update Library Index", command=lambda: self.libraries_panel.update_index())
        tools_menu.add_command(label="Install Core for Current Board", command=self.install_current_core)
        tools_menu.add_command(label="Refresh Boards and Ports", command=self.refresh_all, accelerator="F5")
        tools_menu.add_separator()
        tools_menu.add_command(label="Serial Monitor", command=self.toggle_serial, accelerator="Ctrl+M")
        tools_menu.add_command(label="Integrated Terminal", command=self.toggle_terminal, accelerator="Ctrl+`")
        tools_menu.add_command(label="Open Terminal Here", command=self.open_terminal_here)
        tools_menu.add_command(label="Open Serial Port in Terminal", command=lambda: self._terminal_command("cd ."))
        menubar.add_cascade(label="Tools", menu=tools_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Toggle Project Explorer", command=self.toggle_sidebar, accelerator="Ctrl+B")
        view_menu.add_command(label="Toggle Bottom Panel", command=self.toggle_bottom, accelerator="Ctrl+J")
        view_menu.add_command(label="Console", command=lambda: self._show_bottom("console"))
        view_menu.add_command(label="Serial Monitor", command=lambda: self._show_bottom("serial"))
        view_menu.add_command(label="Terminal", command=lambda: self._show_bottom("terminal"))
        view_menu.add_separator()
        view_menu.add_command(label="Zoom In", command=lambda: self._zoom_editor(1), accelerator="Ctrl++")
        view_menu.add_command(label="Zoom Out", command=lambda: self._zoom_editor(-1), accelerator="Ctrl+-")
        view_menu.add_command(label="Reset Zoom", command=self._reset_zoom, accelerator="Ctrl+0")
        view_menu.add_separator()
        view_menu.add_command(label="Next Tab", command=lambda: self.tabs.cycle(1), accelerator="Alt+Right")
        view_menu.add_command(label="Previous Tab", command=lambda: self.tabs.cycle(-1), accelerator="Alt+Left")
        view_menu.add_separator()
        view_menu.add_command(label="Toggle Line Numbers", command=lambda: self._toggle_setting("show_line_numbers"))
        view_menu.add_command(label="Toggle Word Wrap", command=lambda: self._toggle_setting("word_wrap"))
        view_menu.add_command(label="Toggle Auto-Complete", command=lambda: self._toggle_setting("auto_complete"))
        view_menu.add_command(label="Toggle Dark / Light", command=self.toggle_theme)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Run Setup Wizard Again", command=lambda: self._run_setup_wizard(first_run=False))
        help_menu.add_command(label="Board Manager URLs", command=lambda: self.open_settings("Boards & Libraries"))
        help_menu.add_command(label="Open Settings Folder", command=self._open_config_folder)
        help_menu.add_command(label="Open Log File", command=self._open_log_file)
        help_menu.add_command(label="Arduino Studio Readme", command=self.show_readme)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self._menubar_by_name = {
            "File": file_menu, "Edit": edit_menu, "Sketch": sketch_menu,
            "Tools": tools_menu, "View": view_menu, "Help": help_menu,
        }
        self._rebuild_board_menus()

    def _bind_shortcuts(self) -> None:
        bindings = {
            "<Control-s>": lambda event: self.save(),
            "<Control-S>": lambda event: self.save_all(),
            "<Control-o>": lambda event: self.open_project_dialog(),
            "<Control-Shift-N>": lambda event: self.new_project(),
            "<Control-r>": lambda event: self.verify(),
            "<Control-R>": lambda event: self.verify_clean(),
            "<Control-u>": lambda event: self.upload(),
            "<Control-f>": lambda event: self.find(),
            "<Control-h>": lambda event: self.replace(),
            "<Control-g>": lambda event: self.goto_line(),
            "<Control-m>": lambda event: self.toggle_serial(),
            "<Control-grave>": lambda event: self.toggle_terminal(),
            "<Control-b>": lambda event: self.toggle_sidebar(),
            "<Control-j>": lambda event: self.toggle_bottom(),
            "<Control-comma>": lambda event: self.open_settings(),
            "<Control-Shift-L>": lambda event: self.open_libraries(),
            "<Control-Shift-I>": lambda event: self.open_bootloader(),
            "<Control-Shift-E>": lambda event: self.show_example_picker(),
            "<Control-p>": lambda event: self.show_command_palette(),
            "<Control-w>": lambda event: self.close_tab(),
            "<Control-Shift-U>": lambda event: self.upload_programmer(),
            "<Alt-Right>": lambda event: self.tabs.cycle(1),
            "<Alt-Left>": lambda event: self.tabs.cycle(-1),
            "<Control-Tab>": lambda event: self.tabs.cycle(1),
            "<Control-Shift-Tab>": lambda event: self.tabs.cycle(-1),
            "<Control-0>": lambda event: self._reset_zoom(),
            "<F2>": lambda event: self._editor_action("next_error"),
            "<Shift-F2>": lambda event: self._editor_action("prev_error"),
            "<F5>": lambda event: self.refresh_all(),
            "<F3>": lambda event: self._editor_action("find_next"),
            "<Shift-F3>": lambda event: self._editor_action("find_previous"),
            "<Escape>": lambda event: self._on_escape(),
        }
        for sequence, handler in bindings.items():
            try:
                self.bind_all(sequence, handler)
            except tk.TclError:  # pragma: no cover - unsupported accelerator
                self._log.debug("shortcut %s could not be bound", sequence)

    # ============================================================== view logic
    def _show_view(self, name: str, *, focus: bool = True) -> None:
        widget = self.views.get(name, self.tabs)
        for key, panel in self.views.items():
            if panel is widget:
                continue
            try:
                panel.grid_forget()
            except tk.TclError:  # pragma: no cover
                pass
        try:
            widget.grid(row=0, column=0, sticky="nsew")
        except tk.TclError:  # pragma: no cover
            return
        self._current_view = name if name in self.views else "editor"
        if name == "libraries" and focus:
            self.libraries_panel.focus_search()
        elif name == "bootloader":
            self.bootloader_panel.refresh_from_app()
        elif name == "settings":
            self.settings_view.reload()
        elif focus:
            editor = self.tabs.selected_editor()
            if editor is not None:
                editor.focus_editor()

    def _show_bottom(self, name: str) -> None:
        if name not in self.bottom_panels:
            name = "console"
        for key, panel in self.bottom_panels.items():
            try:
                panel.grid_forget()
            except tk.TclError:  # pragma: no cover
                pass
        panel = self.bottom_panels[name]
        try:
            panel.grid(row=0, column=0, sticky="nsew")
        except tk.TclError:  # pragma: no cover
            return
        self._current_bottom = name
        try:
            self.bottom_tabs.set({"console": "Console", "serial": "Serial Monitor", "terminal": "Terminal"}[name])
        except (tk.TclError, KeyError):  # pragma: no cover
            pass
        if not self._bottom_collapsed:
            self._set_bottom_height(BOTTOM_DEFAULT_HEIGHT)
        if name == "serial":
            self.serial_panel.refresh_ports()
        elif name == "terminal":
            self.terminal_panel.start()
        self._update_bottom_info()

    def _on_bottom_segment(self, label: str) -> None:
        mapping = {"Console": "console", "Serial Monitor": "serial", "Terminal": "terminal"}
        self._show_bottom(mapping.get(str(label), "console"))

    def _clear_bottom(self) -> None:
        if self._current_bottom == "console":
            self.console.clear()
        elif self._current_bottom == "serial":
            self.serial_panel.clear()
        else:
            self.terminal_panel.clear()

    def _update_bottom_info(self) -> None:
        label = ""
        if self._current_bottom == "serial":
            try:
                label = self.serial_panel.stats_label.cget("text")
            except (tk.TclError, AttributeError):  # pragma: no cover
                label = ""
        elif self._current_bottom == "terminal":
            try:
                label = self.terminal_panel.cwd_label.cget("text")
            except (tk.TclError, AttributeError):  # pragma: no cover
                label = ""
        try:
            self.bottom_info.configure(text=label)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def toggle_bottom(self) -> None:
        """Collapse / expand the console + serial + terminal area (Ctrl+J)."""
        self._bottom_collapsed = not self._bottom_collapsed
        if self._bottom_collapsed:
            self._set_bottom_height(44)
            try:
                self.bottom_body.grid_forget()
            except tk.TclError:  # pragma: no cover
                pass
            self.status.set_message("bottom panel collapsed")
        else:
            try:
                self.bottom_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
            except tk.TclError:  # pragma: no cover
                pass
            self._show_bottom(self._current_bottom)
            self.status.set_message("bottom panel expanded")

    def _set_bottom_height(self, height: int) -> None:
        try:
            self.grid_rowconfigure(2, minsize=int(height))
            self._bottom.grid_propagate(False)
            self._bottom.configure(height=int(height))
        except (tk.TclError, ValueError):  # pragma: no cover
            pass

    def toggle_sidebar(self) -> None:
        """Show / hide the explorer column (Ctrl+B)."""
        self._sidebar_visible = not self._sidebar_visible
        width = SIDEBAR_DEFAULT_WIDTH if self._sidebar_visible else 0
        if self._sidebar_visible:
            try:
                self.explorer.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
                self.main_row.grid_columnconfigure(0, weight=0, minsize=width)
            except tk.TclError:  # pragma: no cover
                pass
        else:
            try:
                self.explorer.grid_forget()
                self.main_row.grid_columnconfigure(0, minsize=0)
            except tk.TclError:  # pragma: no cover
                pass

    # ================================================================ projects
    @property
    def has_project(self) -> bool:
        return self.project is not None

    def _sketchbook_parent(self) -> Optional[Path]:
        text = str(self.settings.sketchbook_dir or "").strip()
        if text:
            path = Path(text).expanduser()
            if path.is_dir():
                return path
        return None

    def _new_cli(self) -> Any:
        from ..core.arduino_cli import ArduinoCLI

        return ArduinoCLI(cli_path=self.settings.effective_cli_path(),
                         config_file=self.settings.cli_config_file or "",
                         extra_args=self.settings.cli_extra_args or "")

    def _rebuild_services(self) -> None:
        """Re-create the CLI wrapper (and dependents) after path/data changes."""
        self.cli = self._new_cli()
        self.libraries = LibraryManager(self.cli, data_dir=self.store.data_dir)
        self.bootloader = BootloaderService(self.cli, cli_path=self.settings.effective_cli_path(),
                                           data_dir=self.store.data_dir)
        self.projects = ProjectManager(default_parent=self._sketchbook_parent())
        for setter, value in ((getattr(self.libraries_panel, "manager", None), self.libraries),):
            del setter, value
        try:
            self.libraries_panel.manager = self.libraries
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        try:
            self.bootloader_panel.service = self.bootloader
            self.bootloader_panel.reload_capabilities()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        try:
            self.terminal_panel.restart()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def new_project(self) -> None:
        """File ▸ New Project: ask for name/location/board, then create the tree."""
        labels, selected = self._board_labels_for_dialog()
        result = ask_new_project(
            self, palette=self.palette, manager=self.projects, initial_parent=self._projects_parent(),
            board_labels=labels, selected_board=selected, board_for_label=self._fqbn_for_label,
        )
        if result is None:
            return
        try:
            project = self.projects.create_project(
                result.parent, result.name, board_fqbn=result.board_fqbn or self.fqbn(),
                description=result.description, with_example_files=result.with_example_files,
            )
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not create the project", message=str(exc),
                        palette=self.palette)
            self.status.set_error("project creation failed")
            return
        self._adopt_project(project, open_main=result.open_main)
        self.settings.remember_project(project.root)
        self.settings.update(fqbn=result.board_fqbn or self.settings.fqbn)
        self.store.mark_dirty()
        self._rebuild_recent_menu()
        self.status.finish_operation(True, f"created {project.name}")
        self.log(f"created project {project.root}", "ok")

    def _projects_parent(self) -> Path:
        sketchbook = self._sketchbook_parent()
        if sketchbook is not None:
            return sketchbook
        try:
            return Path(self.projects.suggested_parent())
        except Exception:  # pragma: no cover
            return Path.home()

    def _board_labels_for_dialog(self) -> tuple[list[str], str]:
        labels = list(self._board_labels) or sort_board_labels([profile.name for profile in board_list_profiles()])
        return labels, display_name_for_fqbn(self.fqbn()) or labels[0]

    def _fqbn_for_label(self, label: str) -> str:
        fqbn = self._board_map.get(str(label), "")
        if fqbn:
            return fqbn
        for profile in board_list_profiles():
            if profile.name == label:
                return profile.fqbn
        return self.settings.fqbn or "arduino:avr:uno"

    def new_example(self, example_id: str) -> None:
        """File ▸ New Example ▸ <name>: create the example as a fresh project."""
        example = get_example(str(example_id))
        if example is None:  # pragma: no cover - defensive
            ask_message(self, kind="error", title="Unknown example", message=f"No example '{example_id}'.",
                        palette=self.palette)
            return
        if self.tabs.has_unsaved and not self._save_before_switch():
            return
        name = example_project_name(example)
        parent = self._projects_parent()
        try:
            target = self.projects.unique_project_dir(parent, name)
        except Exception:  # pragma: no cover
            target = parent / name
        answer = ask_message(
            self, kind="question", title=example.title,
            message=f"{example.summary}\n\nCreate the example as '{target.name}'?",
            detail=example.readme or "", buttons=("Cancel", "Create project"), palette=self.palette,
        )
        if answer != "Create project":
            return
        try:
            files = render_files(example, target.name)
            project = self.projects.create_project_from_template(target.parent, target.name, files,
                                                                 example.fqbn_hint, example.summary)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not create the example", message=str(exc),
                        palette=self.palette)
            return
        self._adopt_project(project, open_main=True)
        self.settings.update(fqbn=example.fqbn_hint or self.settings.fqbn)
        if example.serial_baud:
            self.settings.update(serial_baud=int(example.serial_baud))
            self.serial_panel.persist_options()
        self.status.finish_operation(True, f"created example {project.name}")
        self.log(f"example '{example.title}' written to {project.root}", "ok")
        if self.settings.port:
            self.log("the example prints over serial - open the Serial Monitor tab and press Open", "note")

    def show_example_picker(self) -> None:
        """Ctrl+Shift+E: pick any bundled example from a list."""
        from .widgets.dialogs import ask_choice

        ids = [example.id for example in EXAMPLES]
        if not ids:  # pragma: no cover
            return
        chosen = ask_choice(self, ids, title="New Example", label="Which example should be created?",
                            display=lambda item: example_menu_label(get_example(str(item)) or EXAMPLES[0]),
                            palette=self.palette)
        if chosen:
            self.new_example(str(chosen))

    def open_project_dialog(self) -> None:
        """File ▸ Open Project… (folder picker, accepts a .ino too)."""
        from tkinter import filedialog

        initial = str(self.project.root.parent) if self.project is not None else str(self._projects_parent())
        try:
            chosen = filedialog.askdirectory(parent=self, title="Open an Arduino project folder",
                                             initialdir=initial if Path(initial).is_dir() else str(Path.home()))
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen:
            return
        self.open_project(Path(chosen))

    def open_project(self, path: os.PathLike[str] | str, *, quiet: bool = False) -> bool:  # type: ignore[name-defined]
        """Open an existing project folder; ``True`` on success."""
        target = Path(str(path)).expanduser()
        if target.is_file():
            target = target.parent
        if self.tabs.has_unsaved and not self._save_before_switch():
            return False
        try:
            project = self.projects.open(target)
        except (ProjectError, OSError) as exc:
            if not quiet:
                ask_message(self, kind="error", title="Could not open the project", message=str(exc),
                            detail=f"Folder: {target}\nA project needs a <name>.ino file (or an empty folder "
                                   "to start from).", palette=self.palette)
            self.status.set_error("could not open that folder")
            return False
        self._adopt_project(project, open_main=True)
        self.settings.remember_project(project.root)
        self._rebuild_recent_menu()
        self.store.mark_dirty()
        if not quiet:
            problems = []
            try:
                problems = self.projects.validate(project)
            except Exception:  # pragma: no cover
                problems = []
            if problems:
                self.log("project check: " + "; ".join(problems), "warn")
        return True

    def _adopt_project(self, project: Project, *, open_main: bool = True) -> None:
        """Point every panel at *project* (board, files, terminal cwd)."""
        self.project = project
        manifest_board = ""
        try:
            manifest_board = str(project.manifest.board_fqbn or "")
        except Exception:  # pragma: no cover
            manifest_board = ""
        if manifest_board and manifest_board != self.settings.fqbn:
            self.settings.update(fqbn=manifest_board)
        try:
            self.explorer.set_project(project)
            self.explorer.set_main_sketch(project.main_sketch)
            self.explorer.set_empty_state(False)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        try:
            self.tabs.set_project(project, reopen=True)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if open_main:
            main = project.main_sketch
            if main.is_file():
                self.tabs.open_file(main)
        self.status.set_project(project.name, modified=self.tabs.has_unsaved)
        self.status.set_board(display_name_for_fqbn(self.fqbn()) or self.fqbn())
        try:
            self.terminal_panel.set_project(project.root)
            self.terminal_panel.refresh_quick_commands()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self._last_build_dir = None
        self.bootloader_panel.refresh_from_app()
        try:
            self.libraries_panel.refresh()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self._sync_dirty_markers()
        self._set_title()
        self._update_menu_titles()

    def close_project(self) -> None:
        """File ▸ Close Project (asks about unsaved files)."""
        if self.project is None:
            self.status.set_message("no project open")
            return
        if self.tabs.has_unsaved and not self._save_before_switch():
            return
        self.project = None
        try:
            self.tabs.set_project(None, reopen=False)
            self.explorer.set_project(None)
            self.explorer.set_empty_state(True)
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.settings.update(active_project="")
        self.store.mark_dirty()
        self.status.set_project("no project")
        self._last_build_dir = None
        self._set_title()
        self.status.finish_operation(True, "project closed")

    def _on_project_changed(self) -> None:
        """Something touched the tree: refresh the explorer and the manifest."""
        self.refresh_explorer()
        self._sync_dirty_markers()
        if self.project is not None:
            self._save_board_to_manifest(self.fqbn())

    def refresh_explorer(self) -> None:
        try:
            self.explorer.refresh()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self._sync_dirty_markers()

    def _sync_dirty_markers(self) -> None:
        dirty = set()
        try:
            dirty = {Path(item) for item in self.tabs.dirty_files}
        except (AttributeError, TypeError):  # pragma: no cover
            dirty = set()
        try:
            self.explorer.set_dirty_paths(sorted(str(path) for path in dirty))
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.status.set_project(self.project.name if self.project is not None else "no project",
                               modified=bool(dirty))
        self.toolbar.set_save_enabled(bool(dirty) or self.project is not None)

    def _set_title(self) -> None:
        name = self.project.name if self.project is not None else "no project"
        star = "*" if self.tabs.has_unsaved else ""
        try:
            self.title(f"{star}{name} - Arduino Studio")
        except tk.TclError:  # pragma: no cover
            pass

    def _update_menu_titles(self) -> None:
        enabled = self.project is not None
        for label in ("Rename Project…", "Duplicate Project…", "Show Project Folder", "Close Project",
                     "Export Compiled Binary", "Show Build Folder", "Reload Files from Disk"):
            menu = getattr(self, "_menubar_by_name", {}).get("File") if label.endswith("…") or label == "Close Project" else getattr(self, "_menubar_by_name", {}).get("Sketch")
            if menu is None:  # pragma: no cover
                continue
            try:
                menu.entryconfigure(label, state="normal" if enabled else "disabled")
            except (tk.TclError, KeyError, IndexError):  # pragma: no cover
                continue

    def _save_before_switch(self) -> bool:
        """Save / discard / cancel prompt when leaving a dirty project."""
        answer = ask_message(
            self, kind="warning", title="Unsaved changes",
            message="Save the modified files before continuing?",
            detail="\n".join(path.name for path in self.tabs.dirty_files[:12]),
            buttons=("Cancel", "Discard", "Save all"), palette=self.palette,
        )
        if answer == "Save all":
            self.save_all()
            return True
        return answer == "Discard"

    def _unsaved_handler(self, document: Any) -> str:
        """Called by the tab bar when a dirty tab is closed."""
        answer = ask_message(
            self, kind="warning", title="Unsaved changes",
            message=f"Save {document.name} before closing?",
            detail=f"{document.path}\n{document.line_count()} line(s), modified.",
            buttons=("Keep open", "Discard", "Save"), palette=self.palette,
        )
        if answer == "Save":
            return "save"
        if answer == "Discard":
            return "discard"
        return "keep"

    # =================================================================== files
    def save(self) -> bool:
        """Ctrl+S: save the active file."""
        editor = self.tabs.selected_editor()
        if editor is None:
            self.status.set_message("nothing to save")
            self.new_file_inline() if self.project is not None else None
            return False
        ok = bool(self.tabs.save_current())
        if ok:
            document = editor.document
            self.log(f"saved {document.name}", "dim")
            self.status.finish_operation(True, f"saved {document.name}")
            self._after_save(document)
        else:
            self.status.set_error("save failed - see the console")
        self._sync_dirty_markers()
        self._set_title()
        return ok

    def save_all(self) -> int:
        """Ctrl+Shift+S: save every modified file."""
        count = 0
        try:
            count = int(self.tabs.save_all())
        except Exception as exc:  # pragma: no cover
            self._log.exception("save all failed")
            self.status.set_error(f"could not save all files: {exc}")
            return 0
        if count:
            self.log(f"saved {count} file(s)", "dim")
        self.status.finish_operation(True, f"saved {count} file(s)")
        self._sync_dirty_markers()
        self._set_title()
        return count

    def _after_save(self, document: Any) -> None:
        """Re-run the project check after saving the main sketch."""
        self.refresh_explorer()
        if self.project is None:
            return
        try:
            relative = self.project.relative(document.path)
        except Exception:  # pragma: no cover
            relative = ""
        if str(relative).endswith(".ino"):
            self._check_project_structure()

    def _check_project_structure(self) -> None:
        if self.project is None:
            return
        try:
            problems = self.projects.validate(self.project)
        except Exception:  # pragma: no cover
            return
        for problem in problems:
            self.log(f"project check: {problem}", "warn")
        if problems:
            self.status.set_message(f"{len(problems)} structure warning(s) - see console")

    def new_file(self, relative: str = "") -> None:
        """Explorer callback: create a file under *relative* (asked by the tree)."""
        self._create_entry(relative, folder=False)

    def new_folder(self, relative: str = "") -> None:
        self._create_entry(relative, folder=True)

    def new_file_inline(self) -> None:
        """Toolbar/menu entry: create a file in the project root."""
        self._create_entry("", folder=False)

    def _create_entry(self, relative: str, *, folder: bool) -> None:
        if self.project is None:
            self.status.set_message("open or create a project first")
            return
        target_folder = ""
        try:
            target_folder = str(self.explorer.target_folder() or "")
        except (AttributeError, tk.TclError):  # pragma: no cover
            target_folder = ""
        base = Path(relative or target_folder or ".")
        kind = "folder" if folder else "file"
        name = ask_text(self, f"New {kind} name", initial="", title=f"New {kind.capitalize()}",
                        hint="relative to the selected folder", palette=self.palette,
                        confirm_label="Create")
        if not name:
            return
        relative_path = (base / name).as_posix() if str(base) not in (".", "") else name
        try:
            if folder:
                self.projects.create_folder(self.project, relative_path)
            else:
                path = self.projects.create_file(self.project, relative_path, "")
                self.tabs.open_file(path)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title=f"Could not create the {kind}", message=str(exc),
                        palette=self.palette)
            return
        self.refresh_explorer()
        self.log(f"created {relative_path}", "dim")
        self.status.finish_operation(True, f"created {relative_path}")

    def import_file(self) -> None:
        """Copy an external file into the project (Explorer ▸ Import)."""
        if self.project is None:
            self.status.set_message("open or create a project first")
            return
        from tkinter import filedialog

        try:
            chosen = filedialog.askopenfilename(parent=self, title="Add a file to the project",
                                                filetypes=[("Sources", "*.ino *.cpp *.c *.h *.hpp"),
                                                           ("All files", "*.*")])
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen:
            return
        try:
            target = self.projects.import_file(self.project, chosen)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Import failed", message=str(exc), palette=self.palette)
            return
        self.refresh_explorer()
        self.tabs.open_file(target)
        self.log(f"imported {target.name}", "ok")

    def _open_from_explorer(self, path: Path) -> None:
        target = Path(path)
        if target.is_dir():
            self._terminal_command(f'cd "{target}"')
            return
        self.tabs.open_file(target)

    def _rename_from_explorer(self, path: Path, new_name: str) -> None:
        if self.project is None:
            return
        old = Path(path)
        try:
            relative = self.project.relative(old)
            target = self.projects.rename_file(self.project, relative, new_name)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not rename", message=str(exc), palette=self.palette)
            return
        self.tabs.rename_file(old, target)
        self.refresh_explorer()
        self.log(f"renamed {old.name} -> {target.name}", "dim")
        if target.name == f"{self.project.sketch_name}.ino" or target.suffix == ".ino":
            self._check_project_structure()

    def _delete_from_explorer(self, path: Path) -> None:
        if self.project is None:
            return
        target = Path(path)
        name = target.name
        if not ask_yes_no(self, f"Delete {name}?",
                          detail="The file is removed from the project folder. This cannot be undone.",
                          yes_label="Delete", kind="warning", palette=self.palette):
            return
        try:
            removed = self.projects.delete_file(self.project, self.project.relative(target))
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not delete", message=str(exc), palette=self.palette)
            return
        if not removed:
            self.status.set_error(f"{name} was not deleted")
            return
        self.tabs.forget_path(target)
        self.refresh_explorer()
        self.log(f"deleted {name}", "dim")

    def _duplicate_from_explorer(self, path: Path) -> None:
        if self.project is None:
            return
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            ask_message(self, kind="error", title="Could not duplicate", message=str(exc), palette=self.palette)
            return
        stem = source.stem
        target = f"{stem}_copy{source.suffix or '.ino'}"
        try:
            created = self.projects.create_file(self.project, target, text)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not duplicate", message=str(exc), palette=self.palette)
            return
        self.refresh_explorer()
        self.tabs.open_file(created)
        self.log(f"duplicated as {created.name}", "dim")

    def _show_project_info(self) -> None:
        """Small summary of the open project: paths, board, ports, file counts."""
        project = self.project
        if project is None:
            self.status.set_message("no project open")
            return
        manifest = getattr(project, "manifest", None)
        lines: list[str] = []
        lines.append(f"Folder        {project.root}")
        lines.append(f"Main sketch   {project.relative(project.main_sketch)}")
        lines.append(f"project.json  {project.relative(project.manifest_path)}")
        try:
            sketch_count = len(project.sketch_files())
            source_count = len(project.source_files())
        except OSError:  # pragma: no cover - unreadable folder
            sketch_count = source_count = 0
        lines.append(f"Files         {sketch_count} .ino sketch(es), {source_count} editable source file(s)")
        fqbn = self.fqbn()
        board = display_name_for_fqbn(fqbn) or "unknown board"
        lines.append(f"Board         {board}  ({fqbn})")
        lines.append(f"Port          {self.port() or 'not selected'}")
        capacity = sketch_memory_report(fqbn)
        if capacity:
            lines.append(f"Memory        {capacity}")
        lines.append(f"Build folder  {self._last_build_dir or project.build_dir() or 'not built yet'}")
        if manifest is not None:
            description = str(getattr(manifest, "description", "") or "").strip()
            author = str(getattr(manifest, "author", "") or "").strip()
            notes = str(getattr(manifest, "notes", "") or "").strip()
            if description:
                lines.append(f"Description   {description}")
            if author:
                lines.append(f"Author        {author}")
            if notes:
                lines.append(f"Notes         {notes}")
        if self.tabs.dirty_files:
            names = ", ".join(Path(path).name for path in sorted(self.tabs.dirty_files))
            lines.append(f"Unsaved       {names}")
        ask_message(self, kind="info", title=f"{project.name} - project info",
                    message="\n".join(lines), buttons=("Close",), palette=self.palette, width=680)

    def _project_action(self, action: str) -> None:
        """Explorer context menu / File menu entries that act on the project."""
        name = str(action or "")
        if self.project is None and name not in ("reveal",):
            self.status.set_message("no project open")
            return
        if name in ("reveal", "open_folder", "show_folder"):
            open_path_in_file_manager(self.project.root if self.project else self._projects_parent())
            return
        if name == "build_folder":
            folder = self._last_build_dir or (self.project.build_dir() if self.project else None)
            if folder is None or not Path(folder).is_dir():
                self.status.set_message("no build folder yet - run Verify first")
                return
            open_path_in_file_manager(Path(folder))
            return
        if name == "info":
            self._show_project_info()
            return
        if name == "rename":
            self._rename_project()
            return
        if name == "duplicate":
            self._duplicate_project()
            return
        if name == "delete":
            self._delete_project()
            return
        if name == "new_file":
            self.new_file_inline()
            return
        if name == "reload":
            self.reload_from_disk()
            return
        if name == "terminal":
            self._terminal_command(f'cd "{self.project.root}"')
            return
        self._show_view("settings" if name == "settings" else self._current_view)

    def _rename_project(self) -> None:
        if self.project is None:
            return
        old_name = self.project.sketch_name
        name = ask_text(self, "New project name", initial=old_name, title="Rename Project",
                        hint="renames the folder, the .ino file and the manifest", palette=self.palette,
                        confirm_label="Rename")
        if not name or name == old_name:
            return
        try:
            new_root, new_ino = self.projects.rename_project(self.project, name)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not rename the project", message=str(exc),
                        palette=self.palette)
            return
        previous = self.project.root
        self.project = self.projects.open(new_root)
        try:
            self.tabs.forget_path(previous / f"{old_name}.ino")
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.tabs.set_project(self.project, reopen=False)
        self.tabs.open_file(self.project.main_sketch)
        self.settings.remember_project(self.project.root)
        self.settings.forget_project(previous)
        self._rebuild_recent_menu()
        self.refresh_explorer()
        self.terminal_panel.set_project(self.project.root)
        self.log(f"renamed project {old_name} -> {self.project.name}", "ok")
        self.status.finish_operation(True, "project renamed")
        self._set_title()

    def _duplicate_project(self) -> None:
        if self.project is None:
            return
        name = ask_text(self, "Name for the copy", initial=f"{self.project.sketch_name}_copy",
                        title="Duplicate Project", palette=self.palette, confirm_label="Duplicate")
        if not name:
            return
        try:
            copy = self.projects.duplicate_project(self.project, name)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not duplicate the project", message=str(exc),
                        palette=self.palette)
            return
        if ask_yes_no(self, "Open the copy?", detail=f"{copy.name} was created next to the original.",
                      yes_label="Open it", palette=self.palette):
            self._adopt_project(copy, open_main=True)
        self.refresh_explorer()
        self._rebuild_recent_menu()
        self.log(f"duplicated to {copy.root}", "ok")

    def _delete_project(self) -> None:
        if self.project is None:
            return
        trash = sys.platform.startswith("win") or sys.platform == "darwin"
        if not ask_yes_no(self, f"Delete project '{self.project.name}'?",
                         detail="The whole folder moves to the recycle bin." if trash else
                         "The whole folder is deleted permanently.",
                         yes_label="Delete project", kind="error", palette=self.palette):
            return
        try:
            moved = self.projects.delete_project(self.project, to_trash=True)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Could not delete the project", message=str(exc),
                        palette=self.palette)
            return
        self.close_project()
        self.settings.forget_project(moved if isinstance(moved, Path) else self.project or "")
        self._rebuild_recent_menu()
        self.log(f"deleted project folder {moved}", "warn")

    def reload_from_disk(self) -> None:
        """Sketch ▸ Reload: re-read every open file from disk."""
        count = 0
        for editor in self.tabs.iter_editors():
            document = editor.document
            if document.dirty and not ask_yes_no(self, f"Discard changes in {document.name}?",
                                                detail="Reloading replaces the editor contents with the file on disk.",
                                                yes_label="Reload", kind="warning", palette=self.palette):
                continue
            changed, message = document.reload_from_disk()
            editor.refresh_palette(self.palette)
            count += 1 if changed else 0
            if message:
                self.log(f"{document.name}: {message}", "dim")
        self.status.finish_operation(True, f"reloaded {count} file(s)")
        self.refresh_explorer()

    # ================================================================ building
    def fqbn(self) -> str:
        """The FQBN to build with (custom override honoured)."""
        return self.settings.effective_fqbn() or "arduino:avr:uno"

    #: entries the port picker shows when there is nothing to pick
    _PORT_PLACEHOLDERS = ("select a port", "no port", "scanning", "refresh ports", "none")

    def port(self) -> str:
        """Currently selected port address (empty when the picker only shows a placeholder)."""
        label = ""
        try:
            label = self.toolbar.current_port()
        except (AttributeError, tk.TclError):  # pragma: no cover
            label = ""
        label = str(label or "").strip()
        mapped = self._port_map.get(label)
        if mapped:
            return str(mapped)
        address = label.split(" - ")[0].strip() if label else ""
        lowered = address.lower()
        if not address or any(lowered.startswith(word) for word in self._PORT_PLACEHOLDERS):
            return str(self.settings.port or "").strip()
        return address

    def _build_dir_for(self, project: Project) -> Optional[Path]:
        if str(self.settings.build_output_mode or "project") == "temp":
            return Path(tempfile.gettempdir()) / f"arduino-studio-build-{project.sketch_name}"
        return project.build_dir()

    def _prepare_request(self, *, upload: bool, clean: bool = False, programmer: str = "") -> Optional[BuildRequest]:
        """Validate everything a build needs, or explain what is missing."""
        if self.project is None:
            ask_message(self, kind="info", title="No project open",
                        message="Create or open a project first - Verify always compiles the whole project.",
                        detail="File ▸ New Project, or File ▸ Open Project on an existing folder.",
                        palette=self.palette)
            self.status.set_message("no project - nothing to verify")
            return None
        if self.runner.busy(LANE_BUILD):
            self.status.set_error("another build or upload is already running")
            self._show_bottom("console")
            return None
        if self.tabs.has_unsaved:
            if not self._save_before_build():
                return None
        try:
            sketch_dir = self.projects.compile_target(self.project)
        except (ProjectError, OSError) as exc:
            ask_message(self, kind="error", title="Cannot compile this project", message=str(exc),
                        palette=self.palette)
            return None
        fqbn = self.fqbn()
        port = self.port()
        if upload and not port:
            answer = ask_message(
                self, kind="warning", title="No port selected",
                message="Upload needs a port. Plug in the board and refresh the port list?",
                detail="Tools ▸ Refresh Boards and Ports (F5) also works.",
                buttons=("Cancel", "Refresh ports"), palette=self.palette,
            )
            if answer == "Refresh ports":
                self.refresh_ports(then_upload=True)
            return None
        if not self.settings.effective_cli_path():
            self.log("arduino-cli path is not configured - trying auto-detection", "warn")
            self.detect_cli()
        libraries = []
        try:
            for folder in (self.project.libraries_dir, self.project.src_dir):
                if Path(folder).is_dir():
                    libraries.append(Path(folder))
        except (AttributeError, OSError):  # pragma: no cover
            pass
        return BuildRequest(
            sketch_dir=Path(sketch_dir), fqbn=fqbn, build_dir=self._build_dir_for(self.project),
            project=self.project, libraries=tuple(libraries),
            warnings=str(self.settings.compile_warnings or "default"),
            clean=bool(clean or self.settings.clean_build),
            verbose=bool(self.settings.verbose_cli_output),
            upload=upload, port=port, programmer=programmer,
            verify_upload=True, no_reset=False,
        )

    def _save_before_build(self) -> bool:
        """Save modified files before compiling (Arduino IDE does the same)."""
        if not self.tabs.has_unsaved:
            return True
        answer = ask_message(self, kind="question", title="Save before building?",
                            message="Some files have unsaved changes. Verify always compiles what is on disk.",
                            detail="\n".join(path.name for path in self.tabs.dirty_files[:10]),
                            buttons=("Build anyway", "Save all"), palette=self.palette)
        if answer == "Save all":
            self.save_all()
        return True

    def verify(self) -> None:
        """Sketch ▸ Verify (Ctrl+R)."""
        self._start_build(upload=False)

    def verify_clean(self) -> None:
        """Verify with a clean build directory."""
        self._start_build(upload=False, clean=True)

    def upload(self) -> None:
        """Sketch ▸ Upload (Ctrl+U): compile, then flash."""
        self._start_build(upload=True)

    def upload_programmer(self) -> None:
        """Upload through the configured ISP programmer."""
        programmer = str(self.settings.bootloader_programmer or "arduino")
        if not ask_yes_no(self, f"Upload using programmer '{programmer}'?",
                         detail="The sketch is compiled first, then flashed via arduino-cli upload "
                                f"--programmer {programmer}. The board's serial bootloader is bypassed.",
                         yes_label="Upload", kind="warning", palette=self.palette):
            return
        self._start_build(upload=True, programmer=programmer)

    def export_binaries(self) -> None:
        """Sketch ▸ Export Compiled Binary: compile with ``--export-binaries``."""
        request = self._prepare_request(upload=False)
        if request is None:
            return
        self._export_binaries(request)

    def _export_binaries(self, request: BuildRequest) -> None:
        """Compile once more with ``--export-binaries`` and open the result folder."""
        self._set_busy(True, "exporting binaries")
        self._show_bottom("console")
        self.console.start_operation("Exporting binaries…", f"{request.project.sketch_name} for {request.fqbn}")
        argv = ["compile", str(request.sketch_dir), "--fqbn", request.fqbn, "--export-binaries", "--warnings",
                request.warnings]
        if request.build_dir is not None:
            argv += ["--build-path", str(request.build_dir)]
        if request.clean:
            argv.append("--clean")
        if request.verbose:
            argv.append("-v")
        for folder in request.libraries:
            argv += ["--libraries", str(folder)]

        def work(context: Any) -> Any:
            result = self.cli.execute(argv, on_line=context.log, context=context, label="export binaries")
            report = None
            try:
                report = self.cli.analyze_compile(result, request.sketch_dir,
                                                 request.build_dir or request.sketch_dir / "build")
            except Exception as exc:  # pragma: no cover - analysis is best effort
                context.log(f"could not analyse the export output: {exc}", "dim")
            return {"result": result, "report": report}

        def done(result: TaskResult) -> None:
            self._set_busy(False, "")
            payload = result.payload if isinstance(result.payload, dict) else {}
            command = payload.get("result")
            report = payload.get("report")
            ok = bool(getattr(command, "ok", False)) if command is not None else result.ok
            if report is not None:
                ok = bool(report.ok)
                self._publish_diagnostics(list(report.diagnostics or []), request.project)
                self._last_build_dir = Path(report.build_dir) if report.build_dir else self._last_build_dir
            if not ok:
                self.console.finish_operation(False, "export failed")
                self.status.set_error("the exported build failed - see the console")
                self.log(self.cli.humanize_failure(getattr(command, "output", "") or "") or "export failed",
                         "error")
                return
            folder = self._last_build_dir or request.build_dir
            produced: list[Path] = []
            if folder is not None and Path(folder).is_dir():
                produced = sorted(item for item in Path(folder).glob("*")
                                 if item.suffix.lower() in {".bin", ".hex", ".elf"})
            self.console.finish_operation(True, f"exported {len(produced)} file(s)")
            self.status.finish_operation(True, "exported compiled binary")
            for item in produced:
                self.log(f"{item.name}  ({human_bytes(item.stat().st_size)})", "ok")
            if ask_yes_no(self, "Open the export folder?", detail=str(folder or ""), yes_label="Open folder",
                          palette=self.palette):
                if folder is not None:
                    open_path_in_file_manager(Path(folder))

        self.runner.submit("app:export-binaries", work, lane=LANE_BUILD, on_done=done,
                          on_log=self._on_build_log)

    def _start_build(self, *, upload: bool, clean: bool = False, programmer: str = "") -> None:
        request = self._prepare_request(upload=upload, clean=clean, programmer=programmer)
        if request is None:
            return
        self._last_build_dir = request.build_dir
        label = "Upload" if upload else "Verify"
        self._set_busy(True, f"{label} running")
        self._show_bottom("console")
        verb = "Uploading" if upload else "Compiling"
        detail = f"{request.project.sketch_name} for {request.fqbn}"
        if upload:
            detail += f" on {request.port}"
        self.console.start_operation(f"{verb}…", detail)
        self.status.start_operation(f"{label} - {request.project.sketch_name}")
        self.log(f"{verb.lower()} {request.project.sketch_name}.ino ({request.fqbn}"
                 f"{', port ' + request.port if request.port else ''})", "step")
        self._serial_was_open = False
        if upload:
            try:
                self._serial_was_open = bool(self.serial_panel.release_port_for_upload())
            except (AttributeError, tk.TclError):  # pragma: no cover
                self._serial_was_open = False

        def work(context: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            report = self.cli.compile(
                request.sketch_dir, request.fqbn, build_dir=request.build_dir, libraries=request.libraries,
                warnings=request.warnings, clean=request.clean, verbose=request.verbose,
                on_line=context.log, context=context, progress=context.progress,
            )
            payload["report"] = report
            if _upload_allowed(upload, report):
                context.progress(0.98, "uploading")
                payload["upload"] = self.cli.upload(
                    build_dir=Path(report.build_dir or request.build_dir or Path(".")),
                    fqbn=request.fqbn, port=request.port, verify=request.verify_upload,
                    verbose=request.verbose, no_reset=request.no_reset, programmer=request.programmer,
                    baud=_upload_baud(request.fqbn, self.settings.serial_baud),
                    on_line=context.log, context=context, progress=context.progress,
                )
            return payload

        def done(result: TaskResult) -> None:
            self._finish_build(result, request, upload=upload)

        try:
            self.runner.submit(f"app:{label.lower()}", work, lane=LANE_BUILD, on_done=done,
                               on_log=self._on_build_log, on_progress=self._on_build_progress)
        except Exception as exc:  # pragma: no cover
            self._set_busy(False, "")
            self.console.finish_operation(False, "could not start the build")
            self.status.set_error(str(exc))

    def _on_build_log(self, line: str, level: str) -> None:
        try:
            self.console.write(line, level)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _on_build_progress(self, fraction: Optional[float], message: str) -> None:
        try:
            self.console.set_progress(fraction, message)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _finish_build(self, result: TaskResult, request: BuildRequest, *, upload: bool) -> None:
        self._set_busy(False, "")
        payload = result.payload if isinstance(result.payload, dict) else {}
        report: Optional[CompileReport] = payload.get("report")
        upload_result = payload.get("upload")
        if result.cancelled:
            self.console.finish_operation(False, "cancelled")
            self.status.finish_operation(False, "cancelled")
            self.log("build cancelled", "warn")
            self._after_upload_reopen()
            return
        if report is None:
            message = str(result.error or "the build could not be started")
            self.console.finish_operation(False, message.splitlines()[0] if message else "failed")
            self.status.set_error(message.splitlines()[0] if message else "build failed")
            self.log(message, "error")
            self._after_upload_reopen()
            return

        self._last_report = report
        self._last_build_dir = Path(report.build_dir) if report.build_dir else request.build_dir
        # ``CompileReport.diagnostics`` is the parsed :class:`Diagnostics`
        # container (list of messages + memory + missing includes), not a list.
        parsed = getattr(report, "diagnostics", None)
        if parsed is None or not getattr(parsed, "diagnostics", None):
            try:
                reparsed = self.cli.parse_output(report.output or "",
                                                 request.project.root if request.project else ".")
            except Exception:  # pragma: no cover
                reparsed = None
            if reparsed is not None:
                parsed = reparsed
        diagnostics = list(getattr(parsed, "diagnostics", []) or []) if parsed is not None else []
        self._publish_diagnostics(diagnostics, request.project)

        elapsed = float(getattr(report, "duration", 0.0) or 0.0)
        summary = ""
        if report.ok:
            memory = getattr(parsed, "memory", None) if parsed is not None else None
            summary = "compiled"
            if memory is not None:
                summary = (f"flash {human_bytes(memory.flash_bytes)} of {human_bytes(memory.flash_max)} "
                           f"({memory.flash_percent:.1f}%), RAM {human_bytes(memory.ram_bytes)} "
                           f"({memory.ram_percent:.1f}%)")
                self.status.set_memory(f"flash {memory.flash_percent:.1f}%  ram {memory.ram_percent:.1f}%")
            self.console.finish_operation(True, f"{summary} in {human_duration(elapsed)}", elapsed)
            self.status.finish_operation(True, f"Done ({human_duration(elapsed)})")
            self.log(f"Compiled in {human_duration(elapsed)} - {summary}", "ok")
        else:
            explanation = ""
            try:
                explanation = self.cli.humanize_failure(report.output or "")
            except Exception:  # pragma: no cover
                explanation = ""
            self.console.finish_operation(False, "compilation error", elapsed)
            self.status.set_error("compilation failed")
            if explanation:
                self.log(explanation, "note")
            self._handle_build_failure(explanation or (report.output or ""), parsed, request)
            self._after_upload_reopen()
            return

        if upload:
            if upload_result is None:
                self.log("upload did not run (no binary was produced)", "warn")
                self._after_upload_reopen()
                return
            ok = bool(getattr(upload_result, "ok", False))
            self.console.finish_operation(ok, "Sketch uploaded" if ok else "upload failed",
                                         float(getattr(upload_result, "duration", 0.0) or 0.0))
            if ok:
                self.status.finish_operation(True, f"uploaded to {request.port}")
                self.log(f"Upload complete on {request.port}", "ok")
            else:
                output = getattr(upload_result, "output", "") or ""
                text = ""
                try:
                    text = self.cli.humanize_failure(output)
                except Exception:  # pragma: no cover
                    text = ""
                self.status.set_error("upload failed")
                self.log(text or "Upload failed - see the output above.", "error")
                if "serial" in (text or output).lower() or "port" in (text or output).lower():
                    self.log("Tip: close any other program using the port (Serial Monitor of the Arduino IDE, "
                             "a terminal), then press Ctrl+U again.", "note")
        else:
            self.status.finish_operation(True, f"Verified ({human_duration(elapsed)})")
        self._after_upload_reopen()

    def _after_upload_reopen(self) -> None:
        """Tell the serial monitor it may take the port back again."""
        try:
            self.serial_panel.upload_finished()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        if self._serial_was_open and not self.settings.serial_auto_reopen_after_upload:
            answer = ask_message(self, kind="info", title="Upload finished",
                                message="The Serial Monitor was closed for the upload. Re-open it?",
                                buttons=("Leave closed", "Re-open"), palette=self.palette)
            if answer == "Re-open":
                self.serial_panel.open_port(port=self.port())
        self._serial_was_open = False

    def _handle_build_failure(self, text: str, parsed: Any, request: BuildRequest) -> None:
        """Turn common failures into an action (install core / library)."""
        lowered = (text or "").lower()
        missing = []
        try:
            missing = list(getattr(parsed, "missing_includes", []) or [])
        except Exception:  # pragma: no cover
            missing = []
        if missing:
            names = ", ".join(str(item) for item in missing)
            self.log(f"missing include file(s): {names}", "warn")
            self.status.set_error(f"{missing[0]} was not found")
        if missing and self.settings.check_missing_includes:
            try:
                if self.libraries_panel.suggest_install(missing):
                    self.log("installing the library will fix the include; run Verify again afterwards", "note")
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
        core_missing = ("platform" in lowered and "not installed" in lowered) or "cores\\arduino" in lowered
        if core_missing and self.settings.install_missing_cores:
            name = core_install_name(request.fqbn) or core_for_fqbn(request.fqbn) or ""
            hint = ""
            for key, value in CORE_INSTALL_HINTS.items():
                if key and key in (name or request.fqbn):
                    hint = value
                    break
            if ask_yes_no(self, f"Install the '{name or request.fqbn}' platform now?",
                         detail=("arduino-cli core install " + (name or request.fqbn) +
                                 "\nThis downloads the platform from the board manager."
                                 + (f"\n\n{hint}" if hint else "")),
                         yes_label="Install", palette=self.palette):
                self._install_core(name or request.fqbn, reverify=True)
            return
        if not missing and not core_missing:
            first = ""
            for line in (text or "").splitlines():
                if "error" in line.lower():
                    first = line.strip()
                    break
            if first:
                self.log(f"first error: {first}", "error")

    def _install_core(self, name: str, *, reverify: bool = False) -> None:
        """``arduino-cli core install`` in the build lane, then optionally re-verify."""
        if not name:
            return
        self._set_busy(True, f"installing {name}")
        self.console.start_operation(f"Installing {name}…", "downloading the platform index and core")
        self.status.start_operation(f"Installing core {name}")

        def work(context: Any) -> Any:
            urls = list(self.settings.additional_board_urls or [])
            if urls:
                try:
                    self.cli.ensure_additional_urls(urls)
                except Exception as exc:  # pragma: no cover
                    context.log(f"additional urls not configured: {exc}", "warn")
            return self.cli.core_install([name], on_line=context.log, context=context)

        def done(result: TaskResult) -> None:
            self._set_busy(False, "")
            if result.ok:
                self.console.finish_operation(True, f"{name} installed")
                self.status.finish_operation(True, f"{name} installed")
                self.log(f"installed core {name}", "ok")
                self._rebuild_services()
                if reverify:
                    self.verify()
                return
            self.console.finish_operation(False, "core install failed")
            self.status.set_error("could not install the core")
            text = ""
            try:
                text = self.cli.humanize_failure(getattr(result.payload, "output", "") or result.error or "")
            except Exception:  # pragma: no cover
                text = result.error or ""
            self.log(text or "core install failed", "error")

        self.runner.submit(f"app:core-install {name}", work, lane=LANE_BUILD, on_done=done,
                           on_log=self._on_build_log)

    def install_current_core(self) -> None:
        """Tools ▸ Install Core for Current Board."""
        fqbn = self.fqbn()
        name = core_install_name(fqbn) or core_for_fqbn(fqbn) or ""
        if not name:
            ask_message(self, kind="info", title="Unknown platform",
                        message=f"Cannot map '{fqbn}' to a core name.",
                        detail="Install it manually: arduino-cli core install <vendor>:<arch>",
                        palette=self.palette)
            return
        self._install_core(name)

    def clean_cache(self) -> None:
        """Tools ▸ Clean Build Cache (``arduino-cli cache clean``)."""
        if not ask_yes_no(self, "Clear arduino-cli's build cache?",
                         detail="The next Verify recompiles everything (slower, but it rules out cache issues).",
                         yes_label="Clean cache", palette=self.palette):
            return

        def work(context: Any) -> Any:
            return self.cli.cache_clean()

        def done(result: TaskResult) -> None:
            if result.ok:
                self.log("build cache cleaned", "ok")
                self.status.finish_operation(True, "cache cleaned")
            else:
                self.status.set_error(str(result.error or "cache clean failed"))

        self.runner.submit("app:cache-clean", work, on_done=done, on_log=self._on_build_log)

    def _publish_diagnostics(self, diagnostics: list[Diagnostic], project: Optional[Project]) -> None:
        """Send compiler errors to the tabs (gutter) and the console."""
        try:
            self.tabs.set_diagnostics_for_project(list(diagnostics))
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        for item in diagnostics[:40]:
            severity = str(getattr(item, "severity", "error") or "error").lower()
            location = ""
            try:
                location = f"{item.project_relative or Path(str(item.file)).name}:{item.line}:{item.column}"
            except (AttributeError, TypeError):  # pragma: no cover
                location = ""
            level = "errline" if getattr(item, "is_error", False) else "warn"
            text = f"{location}: {item.message}" if location else str(item.message)
            try:
                self.console.write(text, level)
            except (tk.TclError, AttributeError):  # pragma: no cover
                self.log(text, "error" if level == "errline" else "warn")
        count = len(diagnostics)
        if count:
            self.log(f"{count} problem(s) reported", "note")

    def _jump_to(self, file: str, line: int) -> None:
        """Console double-click → open the file at that line."""
        path = Path(str(file))
        if not path.is_absolute() and self.project is not None:
            path = self.project.root / path
        if not path.is_file():
            candidate = self.project.root / Path(str(file)).name if self.project else None
            if candidate is not None and candidate.is_file():
                path = candidate
            else:
                self.status.set_error(f"{file} is not in this project")
                return
        editor = self.tabs.open_file(path)
        if editor is None:  # pragma: no cover
            return
        self._show_view("editor", focus=False)
        try:
            editor.goto_line(int(line))
        except (tk.TclError, ValueError, TypeError):  # pragma: no cover
            pass

    # ============================================================== board/port
    def refresh_all(self) -> None:
        """Toolbar/F5: boards, ports, explorer, index staleness."""
        self.refresh_boards(refresh_ports=True)
        self.refresh_explorer()
        self.status.set_message("refreshed boards and ports")

    def refresh_boards(self, *, refresh_ports: bool = False) -> None:
        """``arduino-cli board listall`` in the query lane."""

        def work(context: Any) -> list[Any]:
            try:
                boards = self.cli.board_listall()
            except CLIError as exc:
                context.log(str(exc), "warn")
                boards = []
            return boards

        def done(result: TaskResult) -> None:
            boards = result.payload or []
            labels: list[str] = []
            mapping: dict[str, str] = {}
            for board in boards:
                name = str(getattr(board, "name", "") or getattr(board, "fqbn", ""))
                fqbn = str(getattr(board, "fqbn", "") or "")
                if not name or not fqbn:
                    continue
                if name in labels:            # several FQBNs share a name (menu options)
                    name = f"{name} ({fqbn})"
                labels.append(name)
                mapping[name] = fqbn
            if not labels:
                labels = sort_board_labels([profile.name for profile in board_list_profiles()])
                mapping = {profile.name: profile.fqbn for profile in board_list_profiles()}
                if not result.ok:
                    self.log("board list unavailable - showing the built-in board profiles", "warn")
                else:
                    self.log("no boards installed - add a board manager URL or install a core in Settings", "warn")
            self._board_labels = labels
            self._board_map = mapping
            selected = display_name_for_fqbn(self.fqbn()) or self.fqbn()
            try:
                self.toolbar.set_boards(labels, selected if selected in labels else (labels[0] if labels else None))
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
            self._rebuild_board_menus()
            if refresh_ports:
                self.refresh_ports()

        self.runner.submit("app:board-listall", work, lane=LANE_QUERY, on_done=done)

    def refresh_ports(self, *, quiet: bool = False, then_upload: bool = False) -> None:
        """Detect usable ports (CLI first, pyserial as a fallback)."""

        def work(context: Any) -> list[PortInfo]:
            ports: list[PortInfo] = []
            try:
                ports = self.cli.upload_port_list(self.fqbn())
            except Exception as exc:  # pragma: no cover
                context.log(f"upload-port list failed: {exc}", "dim")
            if not ports:
                try:
                    ports = self.cli.board_list()
                except Exception as exc:  # pragma: no cover
                    context.log(f"board list failed: {exc}", "dim")
            if not ports:
                try:
                    for info in list_serial_ports():
                        address = str(getattr(info, "name", "") or "")
                        ports.append(PortInfo(address=address, description=str(info.description or ""),
                                             device=address))
                except Exception as exc:  # pragma: no cover
                    context.log(f"pyserial scan failed: {exc}", "dim")
            return ports

        def done(result: TaskResult) -> None:
            ports = result.payload or []
            labels: list[str] = []
            mapping: dict[str, str] = {}
            for port in ports:
                address = str(getattr(port, "address", "") or "")
                if not address:
                    continue
                suffix = str(getattr(port, "board_name", "") or getattr(port, "description", "") or "").strip()
                label = f"{address} - {suffix}" if suffix else address
                if label in labels:
                    continue
                labels.append(label)
                mapping[label] = address
            self._port_labels = labels
            self._port_map = mapping
            selected = self.settings.port or ""
            chosen_label = next((label for label, address in mapping.items() if address == selected), "")
            if not chosen_label and labels:
                chosen_label = next((label for label in labels if label.split(" - ")[0] == self.port()), labels[0])
            try:
                self.toolbar.set_ports(labels, chosen_label or None)
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
            self._rebuild_port_menu(labels)
            try:
                self.serial_panel.refresh_ports()
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
            if not labels and not quiet:
                self.status.set_message("no serial ports found - is the board plugged in and its driver installed?")
            elif not quiet:
                self.status.set_message(f"{len(labels)} port(s) available")
            if then_upload and labels:
                self.upload()

        self.runner.submit("app:port-scan", work, lane=LANE_QUERY, on_done=done)

    def _rebuild_board_menus(self) -> None:
        """Tools ▸ Board (and the toolbar) share the CLI board list."""
        menu = getattr(self, "board_menu", None)
        if menu is None:
            return
        try:
            menu.delete(0, "end")
        except tk.TclError:  # pragma: no cover
            return
        current = self.fqbn()
        for label in (self._board_labels or sort_board_labels([profile.name for profile in board_list_profiles()])):
            fqbn = self._fqbn_for_label(label)
            try:
                menu.add_command(label=("*  " if fqbn == current else "   ") + label,
                                 command=lambda value=fqbn, shown=label: self._apply_board_choice(value, shown))
            except tk.TclError:  # pragma: no cover
                continue
        try:
            menu.add_separator()
            menu.add_command(label="Custom FQBN…", command=self._ask_custom_fqbn)
            menu.add_command(label="Board manager URLs…",
                             command=lambda: self.open_settings("Boards & Libraries"))
        except tk.TclError:  # pragma: no cover
            pass

    def _rebuild_port_menu(self, labels: list[str]) -> None:
        menu = getattr(self, "port_menu", None)
        if menu is None:
            return
        try:
            menu.delete(0, "end")
            current = self.port()
            for label in labels or ["No ports detected"]:
                address = self._port_map.get(label, label.split(" - ")[0])
                menu.add_command(label=("*  " if address == current else "   ") + label,
                                 command=lambda value=address: self._apply_port_choice(value))
            menu.add_separator()
            menu.add_command(label="Rescan ports", command=self.refresh_ports)
        except tk.TclError:  # pragma: no cover
            pass

    def _rebuild_programmer_menu(self) -> None:
        menu = getattr(self, "programmer_menu", None)
        if menu is None:
            return
        try:
            menu.delete(0, "end")
            programmers = []
            try:
                programmers = self.bootloader.programmers()
            except Exception:  # pragma: no cover
                programmers = []
            current = str(self.settings.bootloader_programmer or "")
            for item in programmers[:24] or []:
                menu.add_command(label=("*  " if item.id == current else "   ") + item.label,
                                 command=lambda value=item.id: self.settings.update(bootloader_programmer=value))
            if not programmers:
                menu.add_command(label="(no avrdude programmers found)", command=lambda: None)
            menu.add_separator()
            menu.add_command(label="Open the Bootloader tab", command=self.open_bootloader)
        except tk.TclError:  # pragma: no cover
            pass

    def _rebuild_recent_menu(self) -> None:
        menu = getattr(self, "recent_menu", None)
        if menu is None:
            return
        try:
            menu.delete(0, "end")
            recent = list(self.settings.recent_projects or [])
            for entry in recent[:12]:
                path = Path(str(entry))
                exists = path.is_dir()
                label = ("   " if exists else "✗ ") + (path.name or str(path))
                menu.add_command(label=label, command=lambda value=path: self._open_recent(value))
            if not recent:
                menu.add_command(label="(no recent projects)", command=lambda: None)
            menu.add_separator()
            menu.add_command(label="Clear list", command=self._clear_recent)
        except tk.TclError:  # pragma: no cover
            pass

    def _open_recent(self, path: Path) -> None:
        if not Path(path).is_dir():
            self.settings.forget_project(path)
            self._rebuild_recent_menu()
            self.status.set_error(f"{path} no longer exists")
            return
        self.open_project(path)

    def _clear_recent(self) -> None:
        self.settings.update(recent_projects=[])
        self.store.mark_dirty()
        self._rebuild_recent_menu()

    def _on_board_menu_pick(self, label: str) -> None:
        """Toolbar board dropdown (label in, FQBN stored)."""
        self._apply_board_choice(self._fqbn_for_label(str(label)), str(label))

    def _apply_board_choice(self, fqbn: str, label: str = "") -> None:
        fqbn = str(fqbn or "").strip()
        if not fqbn:
            return
        self.settings.update(fqbn=fqbn)
        self.settings.remember_board(fqbn)
        if self.settings.use_custom_fqbn:
            self.settings.update(use_custom_fqbn=False)
            self.log("cleared the custom-FQBN override (a board was picked from the list)", "dim")
        self.store.mark_dirty()
        self.status.set_board(label or display_name_for_fqbn(fqbn) or fqbn)
        shown = label or display_name_for_fqbn(fqbn)
        if shown:
            try:
                self.toolbar.board_menu.set(shown)
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
        self._rebuild_board_menus()
        try:
            self.bootloader_panel.refresh_from_app()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        if self.project is not None:
            self._save_board_to_manifest(fqbn)
        self.log(f"board set to {fqbn} ({display_name_for_fqbn(fqbn) or 'custom'})", "dim")

    def _save_board_to_manifest(self, fqbn: str) -> None:
        if self.project is None:
            return
        try:
            self.project.manifest.board_fqbn = fqbn
            if self.settings.port:
                self.project.manifest.port = self.settings.port
            payload = json.dumps(self.project.manifest.to_dict(), indent=2) + "\n"
            self.projects.write_file(self.project, self.project.relative(self.project.manifest_path), payload)
        except Exception as exc:  # pragma: no cover
            self._log.debug("manifest board write skipped: %s", exc)

    def _on_port_menu_pick(self, label: str) -> None:
        address = self._port_map.get(str(label), str(label).split(" - ")[0].strip())
        self._apply_port_choice(address)

    def _apply_port_choice(self, port: str) -> None:
        port = str(port or "").strip()
        if port.startswith("No port"):
            port = ""
        self.settings.update(port=port)
        self.store.mark_dirty()
        self.status.set_port(port, connected=bool(self.serial_panel.service and getattr(
            self.serial_panel.service, "holds_port", False)))
        self._rebuild_port_menu(self._port_labels)
        if port and self.project is not None:
            self._save_board_to_manifest(self.fqbn())
            try:
                self.cli.write_build_config_note(self.project.root, self.fqbn(), port)
            except Exception as exc:  # pragma: no cover
                self._log.debug("board attach note skipped: %s", exc)

    def _ask_custom_fqbn(self) -> None:
        """Tools ▸ Board ▸ Custom FQBN… (the escape hatch for exotic boards)."""
        current = self.fqbn()
        value = ask_text(self, "Board FQBN", initial=current, title="Custom board FQBN",
                        hint="e.g. arduino:avr:nano:cpu=atmega328old or esp32:esp32:esp32:FlashSize=4M",
                        palette=self.palette, confirm_label="Use FQBN")
        if not value:
            return
        text = str(value).strip()
        if text.count(":") < 2:
            ask_message(self, kind="warning", title="That does not look like an FQBN",
                        message=f"'{text}' needs at least vendor:architecture:board.",
                        palette=self.palette)
            return
        self.settings.update(custom_fqbn=text, use_custom_fqbn=True)
        self.store.mark_dirty()
        self._apply_board_choice(text, text)
        self.settings.update(custom_fqbn=text, use_custom_fqbn=True)
        self.store.mark_dirty()
        self.log(f"custom FQBN enabled: {text}", "ok")

    def detect_cli(self) -> None:
        """Settings/toolbar helper: search for arduino-cli and store what we find."""

        def work(context: Any) -> str:
            found = self.cli.auto_detect()
            context.log(f"searched: {', '.join(map(str, _search_dirs_preview()))}" if not found else
                        f"arduino-cli found at {found}", "dim")
            return found

        def done(result: TaskResult) -> None:
            found = str(result.payload or "")
            if found:
                self.settings.update(arduino_cli_path=found, first_run_completed=True)
                self.store.mark_dirty()
                self._rebuild_services()
                self.settings_view.reload()
                self.settings_view.set_cli_status(f"arduino-cli found at {found}", True)
                self.log(f"arduino-cli detected: {found}", "ok")
                self._probe_cli()
                return
            self.settings_view.set_cli_status(
                "arduino-cli was not found - download it or set the path in Settings ▸ Arduino CLI", False)
            ask_message(self, kind="warning", title="arduino-cli not found",
                        message="No arduino-cli executable was found in PATH or the usual install folders.",
                        detail="Install the Arduino IDE (which ships arduino-cli) or download the CLI from "
                               "https://docs.arduino.cc/arduino-cli/, then set the path in Settings ▸ Arduino CLI.",
                               palette=self.palette)
            self.open_settings("Arduino CLI")

        self.runner.submit("app:detect-cli", work, lane=LANE_QUERY, on_done=done)

    # =============================================================== editors
    def _on_active_file(self, path: Optional[Path]) -> None:
        self._sync_dirty_markers()
        if path is None:
            self.status.set_message("no file open")
            return
        try:
            relative = self.project.relative(path) if self.project else Path(path).name
        except Exception:  # pragma: no cover
            relative = Path(path).name
        self.status.set_message(str(relative))
        self.explorer.select_path(path)

    def find(self) -> None:
        """Ctrl+F: focus find, seeded with the current selection."""
        editor = self.tabs.selected_editor()
        if editor is None:
            self.status.set_message("open a file first")
            return
        editor.show_find()
        self._seed_find_bar(editor, replace=False)

    def replace(self) -> None:
        """Ctrl+H: focus replace, seeded with the current selection."""
        editor = self.tabs.selected_editor()
        if editor is None:
            self.status.set_message("open a file first")
            return
        editor.show_replace()
        self._seed_find_bar(editor, replace=True)

    def _seed_find_bar(self, editor: Any, *, replace: bool) -> None:
        """Pre-fill the find field with the selection (the Arduino IDE does this too)."""
        selection = ""
        try:
            selection = str(editor.text.get("sel.first", "sel.last"))
        except (tk.TclError, AttributeError):  # pragma: no cover
            selection = ""
        try:
            editor.find_bar.show(selection, replace=replace)
        except (tk.TclError, AttributeError, TypeError):  # pragma: no cover
            pass

    def goto_line(self) -> None:
        editor = self.tabs.selected_editor()
        if editor is None:
            return
        lines = 0
        try:
            lines = int(editor.text.index("end-1c").split(".")[0])
        except (tk.TclError, AttributeError, ValueError):  # pragma: no cover
            lines = 0
        value = ask_text(self, "Line number", initial="", title="Go to Line",
                        hint=f"the file has {lines} line(s)", palette=self.palette, confirm_label="Go")
        if not value:
            return
        try:
            editor.goto_line(int(str(value).strip()))
        except (tk.TclError, ValueError):  # pragma: no cover
            self.status.set_error(f"'{value}' is not a line number")

    def _editor_action(self, action: str) -> None:
        """Menu entries that forward to the active editor."""
        editor = self.tabs.selected_editor()
        if editor is None:
            self.status.set_message("no editor in focus")
            return
        name = str(action or "")
        handlers: dict[str, Callable[[], Any]] = {
            "undo": editor.undo, "redo": editor.redo,
            "find_next": editor.find_next, "find_previous": editor.find_previous,
            "highlight_all": lambda: editor.highlight_all(editor.find_bar.options()),
            "comment": editor.toggle_comment, "duplicate": editor.duplicate_line,
            "delete_line": editor.delete_line, "indent": editor.indent_selection,
            "outdent": editor.outdent_selection, "bracket": editor.jump_matching_bracket,
            "select_all": editor.select_all, "completions": editor.show_completions,
            "next_error": editor.next_error, "prev_error": editor.previous_error,
        }
        handler = handlers.get(name)
        if handler is None:  # pragma: no cover
            return
        try:
            handler()
        except (tk.TclError, AttributeError) as exc:  # pragma: no cover
            self._log.debug("editor action %s failed: %s", name, exc)

    def _insert_include(self, header: str) -> None:
        """Library panel callback: insert ``#include <header>`` in the editor."""
        editor = self.tabs.selected_editor()
        if editor is None:
            if self.project is not None:
                self.tabs.open_file(self.project.main_sketch)
                editor = self.tabs.selected_editor()
        if editor is None:
            self.status.set_message("open the .ino file first")
            return
        line = f"#include <{header}>"
        text = editor.content()
        if line in text:
            self.status.set_message(f"{line} is already in the file")
            return
        editor.insert_text(line + "\n", move_caret=False)
        self.status.finish_operation(True, f"added {line}")

    def _zoom_editor(self, direction: int) -> None:
        size = int(self.settings.editor_font_size or 12)
        size = max(6, min(40, size + (direction or 0)))
        self.settings.update(editor_font_size=size)
        self.store.mark_dirty()
        try:
            self.tabs.apply_editor_settings()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def close_tab(self) -> None:
        """File ▸ Close Tab (Ctrl+W) - closes the selected editor tab only."""
        editor = self.tabs.selected_editor()
        path = getattr(getattr(editor, "document", None), "path", None)
        if path is None:
            self.status.set_message("no tab to close")
            return
        try:
            self.tabs.close_file(path)
        except tk.TclError as exc:  # pragma: no cover - widget teardown race
            self._log.warning("close tab failed: %s", exc)

    def _reset_zoom(self) -> None:
        """View ▸ Reset Zoom (Ctrl+0) - back to the configured editor size."""
        default = 12
        if int(self.settings.editor_font_size or default) == default:
            self.status.set_message(f"editor zoom already at {default} pt")
            return
        self.settings.update(editor_font_size=default)
        self.store.mark_dirty()
        try:
            self.tabs.apply_editor_settings()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def _toggle_setting(self, key: str) -> None:
        current = bool(getattr(self.settings, key, False))
        self.settings.update(**{key: not current})
        self.store.mark_dirty()
        try:
            self.settings_view.reload()
            self.tabs.apply_editor_settings()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.status.set_message(f"{key.replace('_', ' ')}: {'on' if not current else 'off'}")

    # ================================================================ panels
    def open_libraries(self) -> None:
        """Toolbar ▸ Libraries (Ctrl+Shift+L)."""
        self._show_view("libraries")
        try:
            self.libraries_panel.refresh()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.status.set_message("Library Manager - search the index, install into the sketchbook or project")

    def open_bootloader(self) -> None:
        """Tools ▸ Burn Bootloaders screen."""
        self._show_view("bootloader")
        self.bootloader_panel.reload_capabilities()
        self.bootloader_panel.refresh_from_app()
        self._rebuild_programmer_menu()
        self.status.set_message("Bootloader tools - burn, read, back up and flash chips over ISP or serial")

    def _bootloader_action(self, action: str) -> None:
        self.open_bootloader()
        name = str(action or "")
        target = {"read": "read_chip_info", "backup": "backup_firmware", "flash": "flash_binary",
                  "erase": "erase_flash"}.get(name)
        if target is None:
            return
        handler = getattr(self.bootloader_panel, target, None)
        if callable(handler):
            self.after(120, handler)

    def burn_bootloader(self) -> None:
        """Tools ▸ Burn Bootloader… (the panel does the confirmation dialog)."""
        self._bootloader_action("burn")
        handler = getattr(self.bootloader_panel, "burn_bootloader", None)
        if callable(handler):
            self.after(200, handler)

    def open_settings(self, tab: str = "") -> None:
        """Toolbar ▸ Settings (Ctrl+,)."""
        self._show_view("settings")
        if tab:
            self.settings_view.focus_tab(str(tab))
        self.settings_view.reload()
        self._rebuild_programmer_menu()
        self.status.set_message("Settings - changes apply as you edit them")

    def toggle_serial(self) -> None:
        """Show the Serial Monitor tab (and start it if it is hidden)."""
        if self._current_view != "editor":
            self._show_view("editor")
        self._show_bottom("serial")
        self.serial_panel.focus_send()

    def toggle_terminal(self) -> None:
        """Ctrl+`: show / hide the Integrated Terminal tab."""
        if self._bottom_collapsed or self._current_bottom != "terminal":
            if self._bottom_collapsed:
                self.toggle_bottom()
            self._show_bottom("terminal")
            self.terminal_panel.focus_command()
            return
        self._show_bottom("console")

    def open_terminal_here(self) -> None:
        """Tools ▸ Open Terminal Here: terminal in the project (or build) folder."""
        if self._bottom_collapsed:
            self.toggle_bottom()
        self._show_bottom("terminal")
        folder = self.project.root if self.project is not None else None
        if folder is not None:
            self.terminal_panel.set_project(folder)
        self.terminal_panel.start(force=True)
        self.terminal_panel.focus_command()

    def _terminal_command(self, command: str) -> None:
        if self._bottom_collapsed:
            self.toggle_bottom()
        self._show_bottom("terminal")
        text = str(command or "").strip()
        if not text:
            return
        try:
            self.terminal_panel.entry.delete(0, "end")
            self.terminal_panel.entry.insert(0, text)
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self.terminal_panel.run_command(text, skip_confirm=False)

    def _on_serial_state(self, open_state: bool) -> None:
        connected = bool(open_state)
        try:
            self.status.set_port(self.port() or ("serial" if connected else ""), connected=connected)
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        self._update_bottom_info()
        self._update_serial_button()

    def _on_escape(self) -> None:
        """Escape closes find/replace bars first, otherwise just blurs."""
        editor = self.tabs.selected_editor()
        if editor is not None and getattr(editor.find_bar, "visible", False):
            editor.find_bar.hide()
            return
        if self._current_view in ("libraries", "bootloader", "settings"):
            self._show_view("editor")

    # ================================================================ helpers
    def _on_setting_changed(self, key: str, value: Any) -> None:
        """React to a settings change (from the view, a panel or a menu)."""
        try:
            if key in ("appearance_mode", "accent_color"):
                self._apply_theme_to_children()
            elif key in ("editor_font_family", "editor_font_size", "tab_size", "insert_spaces", "auto_indent",
                        "auto_close_brackets", "auto_close_quotes", "auto_complete", "highlight_current_line",
                        "show_line_numbers", "word_wrap", "trim_trailing_ws_on_save", "ensure_final_newline_on_save",
                        "show_minimap_gutter"):
                self.tabs.apply_editor_settings()
            elif key in ("console_font_size", "console_max_lines", "console_timestamps"):
                self.console.set_palette(self.palette)
            elif key in ("terminal_shell", "terminal_font_size", "terminal_confirm_destructive"):
                self.terminal_panel.refresh_quick_commands()
                self.terminal_panel.restart()
            elif key in ("serial_baud", "serial_line_ending", "serial_timestamps", "serial_autoscroll",
                        "serial_show_rx", "serial_show_tx", "serial_hex_display", "serial_toggle_dtr",
                        "serial_toggle_rts", "serial_font_size"):
                self.serial_panel.persist_options()
            elif key in ("arduino_cli_path", "cli_config_file", "cli_extra_args", "arduino_data_dir",
                        "auto_detect_cli"):
                self._rebuild_services()
            elif key == "autosave_interval_sec":
                self._restart_timers()
            elif key in ("fqbn", "use_custom_fqbn", "custom_fqbn"):
                self.status.set_board(display_name_for_fqbn(self.fqbn()) or self.fqbn())
                self._rebuild_board_menus()
                try:
                    self.bootloader_panel.refresh_from_app()
                except (AttributeError, tk.TclError):  # pragma: no cover
                    pass
            elif key == "port":
                self.status.set_port(str(value or ""), connected=False)
            elif key == "sketchbook_dir":
                self.projects = ProjectManager(default_parent=self._sketchbook_parent())
            elif key == "additional_board_urls":
                self.log("board manager URLs updated - run 'Update Library Index' to fetch the new indexes", "note")
            elif key == "bootloader_programmer":
                self._rebuild_programmer_menu()
        except Exception as exc:  # pragma: no cover - a bad setting must never crash the UI
            self._log.exception("could not apply setting %s: %s", key, exc)

    def _apply_theme_to_children(self) -> None:
        """Re-skin every panel after a theme / accent change."""
        self.palette = self._palette_from_settings()
        apply_tk_theme(self, self.palette)
        for widget in (getattr(self, "toolbar", None), getattr(self, "explorer", None), getattr(self, "tabs", None),
                       getattr(self, "libraries_panel", None), getattr(self, "bootloader_panel", None),
                       getattr(self, "settings_view", None), getattr(self, "serial_panel", None),
                       getattr(self, "terminal_panel", None), getattr(self, "status", None),
                       getattr(self, "console", None)):
            if widget is None:
                continue
            for name in ("refresh_palette", "set_palette"):
                handler = getattr(widget, name, None)
                if callable(handler):
                    try:
                        handler(self.palette)
                    except (tk.TclError, AttributeError):  # pragma: no cover
                        pass
                    break
        for panel in list(getattr(self, "views", {}).values()) + list(getattr(self, "bottom_panels", {}).values()):
            handler = getattr(panel, "refresh_palette", None)
            if callable(handler) and panel not in (self.toolbar, self.explorer, self.tabs, self.libraries_panel,
                                                   self.bootloader_panel, self.settings_view, self.serial_panel,
                                                   self.terminal_panel, self.status, self.console):
                try:
                    handler(self.palette)
                except (tk.TclError, AttributeError):  # pragma: no cover
                    pass

    def toggle_theme(self) -> None:
        """View ▸ Toggle Dark / Light."""
        current = str(self.settings.appearance_mode or "Dark")
        if current == "System":
            current = "Dark"
        self.settings.update(appearance_mode="Light" if current == "Dark" else "Dark")
        self.store.mark_dirty()

    def _post(self, action: Callable[[], Any]) -> None:
        """Marshal *action* onto the Tk thread.

        :class:`TaskRunner` and the serial/terminal panels are handed a
        ``post(callable)`` hook, so this adapts Tk's ``after(delay, func)`` to
        that contract (calling ``self.after`` directly would pass the callback
        as the delay and silently drop it).
        """
        try:
            self.after(0, action)
        except (tk.TclError, RuntimeError):  # pragma: no cover - window is gone
            pass

    def log(self, message: str, level: str = "info") -> None:
        """Write to the console panel and the log file."""
        text = str(message or "")
        if not text:
            return
        try:
            self.console.write_block(text, level)
        except (AttributeError, tk.TclError):  # pragma: no cover - very early messages
            pass
        getattr(self._log, {"error": "error", "warn": "warning", "ok": "info"}.get(level, "info"))(text)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        try:
            self.toolbar.set_busy(bool(busy))
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        if busy and label:
            try:
                self.status.set_message(f"{label}…")
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
        self._update_progress_visibility(bool(busy))

    def _update_progress_visibility(self, busy: bool) -> None:
        """Mirror the running state in the bottom bar (there is no toolbar gauge)."""
        try:
            if busy:
                self.bottom_info.configure(text="task running…")
            else:
                self._update_bottom_info()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def _cancel_current(self) -> None:
        """Cancel the running build (or the last query) task."""
        handle = None
        try:
            handle = self.runner.current(LANE_BUILD)
            if handle is None:
                handle = self.runner.current(LANE_QUERY)
        except Exception:  # pragma: no cover
            handle = None
        if handle is None:
            self.status.set_message("nothing to cancel")
            return
        try:
            ok = self.runner.cancel(handle)
        except Exception as exc:  # pragma: no cover
            ok = False
            self._log.debug("cancel failed: %s", exc)
        if ok:
            self.status.set_message("cancelling…")
            self.log("cancellation requested", "warn")
        else:
            self.status.set_message("the operation already finished")

    def _save_console_log(self, content: str) -> None:
        from tkinter import filedialog

        suggested = f"{(self.project.name if self.project else 'arduino-studio')}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        try:
            path = filedialog.asksaveasfilename(parent=self, title="Save console output", defaultextension=".log",
                                                initialfile=suggested,
                                                filetypes=[("Log files", "*.log"), ("Text", "*.txt")])
        except tk.TclError:  # pragma: no cover
            path = ""
        if not path:
            return
        try:
            Path(path).write_text(str(content), encoding="utf-8")
        except OSError as exc:
            ask_message(self, kind="error", title="Could not write the log", message=str(exc), palette=self.palette)
            return
        self.status.finish_operation(True, f"console saved to {Path(path).name}")

    def _update_serial_button(self) -> None:
        """Keep the toolbar's serial button readable (open vs closed)."""
        try:
            self.toolbar.set_handler("serial", self.toggle_serial)
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def _on_status_click(self) -> None:
        if self.project is None:
            self.open_project_dialog()
            return
        self._show_view("editor")
        self._show_bottom("console")

    def show_command_palette(self) -> None:
        """Ctrl+P: jump straight to any action by name (a VS Code habit)."""
        from .widgets.dialogs import ask_choice

        actions = {
            "Verify (compile)": self.verify,
            "Upload": self.upload,
            "New Project": self.new_project,
            "Open Project": self.open_project_dialog,
            "New Example": self.show_example_picker,
            "Save All": self.save_all,
            "Burn Bootloader": self.open_bootloader,
            "Manage Libraries": self.open_libraries,
            "Serial Monitor": self.toggle_serial,
            "Integrated Terminal": self.toggle_terminal,
            "Settings": self.open_settings,
            "Refresh Boards and Ports": self.refresh_all,
            "Show Build Folder": lambda: self._project_action("build_folder"),
            "Open Project Folder": lambda: self._project_action("reveal"),
            "Run Setup Wizard": lambda: self._run_setup_wizard(first_run=False),
            "Quit": self.quit_app,
        }
        actions = {key: value for key, value in actions.items() if key}
        chosen = ask_choice(self, list(actions), title="Command Palette", label="Run which command?",
                            palette=self.palette)
        handler = actions.get(str(chosen or ""))
        if handler is not None:
            handler()

    def _open_config_folder(self) -> None:
        open_path_in_file_manager(self.store.directory)

    def _open_log_file(self) -> None:
        folder = self.store.log_dir
        try:
            logs = sorted(Path(folder).glob("*.log*"), key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:  # pragma: no cover
            logs = []
        if not logs:
            self.status.set_message(f"no log files in {folder}")
            return
        open_path_in_file_manager(logs[0])

    def show_readme(self) -> None:
        """Help ▸ Readme: show the packaged README in a viewer dialog."""
        from .widgets.text_view import ScrolledTextView
        from .widgets.dialogs import StudioDialog

        candidates = [Path(__file__).resolve().parent.parent.parent / "README.md",
                     Path(__file__).resolve().parent.parent / "README.md"]
        text = ""
        for candidate in candidates:
            if candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    break
                except OSError:  # pragma: no cover
                    continue
        if not text:
            text = ("Arduino Studio\n\n"
                    "Verify/Upload run arduino-cli; the Serial Monitor and Terminal are integrated.\n"
                    "See the README.md next to the program for setup and board configuration.")

        dialog = StudioDialog(self, title="Arduino Studio - Readme", width=880, height=620,
                              palette=self.palette, resizable=True)
        body = ctk.CTkFrame(dialog, fg_color=self.palette.window_bg, corner_radius=0)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        view = ScrolledTextView(body, self.palette, readonly=True, wrap="word",
                               font=(self.palette.mono_family, 10), bg=self.palette.console_bg,
                               fg=self.palette.console_fg)
        view.grid(row=0, column=0, sticky="nsew")
        view.text.insert("1.0", text)
        ctk.CTkButton(dialog, text="Close", width=110, height=30, command=dialog.close).pack(pady=(0, 12))
        dialog.run()

    def show_about(self) -> None:
        """Help ▸ About."""
        cli_line = "arduino-cli: not checked yet"
        self_status = ""
        try:
            info = self.cli.probe(self.settings.effective_cli_path())
            if getattr(info, "ok", False):
                cli_line = f"arduino-cli {info.version} at {info.path}"
            else:
                cli_line = f"arduino-cli unavailable: {getattr(info, 'message', '') or 'not found'}"
        except Exception as exc:  # pragma: no cover
            cli_line = f"arduino-cli probe failed: {exc}"
        self_status = (f"data: {self.store.data_dir}\nsettings: {self.store.path}\n"
                       f"logs: {self.store.log_dir}\nproject: "
                       f"{self.project.root if self.project else 'none'}")
        ask_message(self, kind="info", title=f"Arduino Studio {_version_string()}",
                    message="A standalone Arduino IDE driven by arduino-cli.\n"
                            "Boards, libraries, serial monitor, terminal and bootloader tools.",
                    detail=f"{cli_line}\n{self_status}", palette=self.palette, width=620)

    # =================================================================== quit
    def quit_app(self) -> None:
        """Save state, close the hardware, then destroy the window."""
        if self._closing:
            return
        if self.tabs.has_unsaved:
            answer = ask_message(self, kind="warning", title="Unsaved changes",
                                message="Save the modified files before quitting?",
                                detail="\n".join(path.name for path in self.tabs.dirty_files[:10]),
                                buttons=("Discard and quit", "Cancel", "Save and quit"), palette=self.palette)
            if answer == "Cancel" or answer is None:
                return
            if answer == "Save and quit":
                self.save_all()
        self._closing = True
        self._remember_window()
        self._remember_session_files()
        try:
            self.serial_panel.close_port()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        try:
            self.terminal_panel.stop()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass
        try:
            self.store.save()
        except Exception:  # pragma: no cover
            self._log.exception("settings could not be saved on exit")
        # A query-lane task (the port scan in particular) is not cancelled by
        # shutdown(), it simply outlives the window and then reports into
        # widgets that no longer exist.  Give it a bounded moment to finish,
        # then stop the runner; the timeout keeps a stubborn subprocess from
        # turning "quit" into a hang.
        try:
            self.runner.wait_all(timeout=2.0)
        except Exception:  # pragma: no cover - defensive
            self._log.debug("waiting for background tasks raised", exc_info=True)
        try:
            self.runner.shutdown()
        except Exception:  # pragma: no cover - defensive
            self._log.debug("runner shutdown raised", exc_info=True)
        for ident in self._timers:
            try:
                self.after_cancel(ident)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        self._log.info("shutting down after %s", human_duration(time.monotonic() - self._app_start))
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def _remember_window(self) -> None:
        try:
            state = str(self.state())
        except tk.TclError:  # pragma: no cover
            state = "normal"
        geometry = ""
        try:
            geometry = str(self.geometry())
        except tk.TclError:  # pragma: no cover
            geometry = ""
        width, height, x, y = _parse_geometry(geometry)
        self.settings.update(window_width=width or int(self.settings.window_width or 1320),
                            window_height=height or int(self.settings.window_height or 820),
                            window_x=max(0, x), window_y=max(0, y),
                            window_maximized=state in ("zoomed", "maximized"))
        self.store.mark_dirty()

    def _remember_session_files(self) -> None:
        paths: list[str] = []
        try:
            paths = [str(path) for path in self.tabs.open_paths()]
        except (AttributeError, TypeError):  # pragma: no cover
            paths = []
        self.settings.update(open_files=paths[:32],
                            active_project=str(self.project.root) if self.project is not None else "")
        self.store.mark_dirty()


# ------------------------------------------------------------------- helpers
def _upload_allowed(upload: bool, report: CompileReport) -> bool:
    """Only flash when the compile step actually produced a binary."""
    if not upload:
        return False
    if not bool(getattr(report, "ok", False)):
        return False
    build_dir = str(getattr(report, "build_dir", "") or "")
    if not build_dir:
        return False
    return Path(build_dir).is_dir()


def _log_level(name: str) -> int:
    """Translate a level name into the numeric level ``setup_logging`` wants."""
    return getattr(logging, str(name or "INFO").upper(), logging.INFO)


def _version_string() -> str:
    from .. import __version__

    return __version__


def _upload_baud(fqbn: str, configured: int) -> Optional[int]:
    """Baud for the uploader; ``None`` keeps the platform default.

    AVR boards upload at their bootloader's rate (Uno 115200, Nano 57600, ...),
    which the board profile already knows, so the CLI value wins.
    """
    board = board_for_fqbn(fqbn)
    board_baud = int(getattr(board, "default_baud", 0) or 0) if board is not None else 0
    return board_baud or baud_for_fqbn(fqbn) or None


def _appearance_for_ctk(mode: str) -> str:
    text = str(mode or "Dark")
    return text.lower() if text in ("Dark", "Light") else "system"


def _parse_geometry(geometry: str) -> tuple[int, int, int, int]:
    """``"1320x820+40+30"`` -> ``(1320, 820, 40, 30)`` with ``0`` for missing parts."""
    width = height = 0
    x = y = -1
    try:
        head, _, rest = str(geometry).partition("+")
        if "x" in head:
            w_text, _, h_text = head.partition("x")
            width, height = int(w_text), int(h_text)
        if rest:
            parts = rest.replace("+-", "-").split("-") if "-" in rest else rest.split("+")
        else:
            parts = []
        if len(parts) >= 2:
            x, y = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):  # pragma: no cover
        pass
    return width, height, x, y


def board_list_profiles() -> list[Any]:
    """The built-in board profiles (used before the CLI list is available)."""
    return list(BUILTIN_BOARDS)


def _search_dirs_preview() -> list[Path]:
    from ..core.process import cli_search_dirs

    try:
        return list(cli_search_dirs())
    except Exception:  # pragma: no cover
        return []


def create_app(argv: Optional[list[str]] = None) -> ArduinoStudioApp:
    """Build the application window from command line style options."""
    import argparse

    parser = argparse.ArgumentParser(prog="arduino-studio", add_help=False)
    parser.add_argument("project", nargs="?", default="")
    parser.add_argument("--config-dir", default="")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args, _unknown = parser.parse_known_args(list(argv or []))
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else None
    open_path = Path(args.project).expanduser() if args.project else None
    return ArduinoStudioApp(config_dir=config_dir, open_path=open_path, force_setup=bool(args.setup),
                            skip_setup=bool(args.no_setup), log_level=str(args.log_level))
