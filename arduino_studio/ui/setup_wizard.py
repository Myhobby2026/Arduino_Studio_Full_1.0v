"""First-run setup wizard.

Shown when ``first_run_completed`` is false (or when *Settings → Arduino CLI →
Test* reports a missing CLI).  Four steps, each of which can be skipped except
the CLI location, because without ``arduino-cli`` the app cannot compile:

1. welcome / what the app needs,
2. find ``arduino-cli`` (auto-detect, manual browse, live test),
3. data + sketchbook folders and optional board manager URLs,
4. board and port (read from the CLI when it answered).

The wizard writes straight into the :class:`Settings` object through the store,
so the main window simply re-reads its services after :meth:`run` returns.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.arduino_cli import ArduinoCLI, CLIInfo
from ..core.boards import BOARD_MANAGER_URLS, BUILTIN_BOARDS, display_name_for_fqbn, sort_board_labels
from ..core.settings import Settings, SettingsStore
from ..core.utils import get_logger
from .theme import Palette, apply_tk_theme
from .widgets.dialogs import ask_message

__all__ = ["SetupWizard", "run_first_run_wizard"]

_STEPS = ("Welcome", "arduino-cli", "Folders", "Board & port")


class SetupWizard(ctk.CTkToplevel):
    """Modal-ish four step setup dialog for a first start."""

    def __init__(self, master: Any, palette: Palette, *, store: SettingsStore, settings: Settings,
                 runner: Any = None, on_board_picked: Optional[Callable[[str], Any]] = None) -> None:
        super().__init__(master)
        self.title("Arduino Studio - first-run setup")
        self.geometry("720x560")
        self.minsize(640, 480)
        apply_tk_theme(self, palette)
        try:
            self.transient(master)
        except tk.TclError:  # pragma: no cover
            pass
        self.palette = palette
        self._store = store
        self._settings = settings
        self._runner = runner
        self._on_board_picked = on_board_picked
        self._log = get_logger("ui.wizard")

        self._step = 0
        self._cli_info: Optional[CLIInfo] = None
        self._boards: list[str] = []
        self._ports: list[str] = []
        self._finished = False
        self._frames: dict[str, ctk.CTkFrame] = {}

        self._build()
        self._show_step(0)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.header = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_columnconfigure(0, weight=1)
        self.step_label = ctk.CTkLabel(self.header, text="", font=(palette.font_family, 14, "bold"),
                                       text_color=palette.text, anchor="w", justify="left")
        self.step_label.grid(row=0, column=0, sticky="w", padx=18, pady=(14, 2))
        self.sub_label = ctk.CTkLabel(self.header, text="", font=(palette.font_family, 10),
                                      text_color=palette.text_muted, anchor="w", justify="left", wraplength=640)
        self.sub_label.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 14))

        body = ctk.CTkFrame(self, fg_color=palette.window_bg, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self._body = body
        for name in _STEPS:
            frame = ctk.CTkFrame(body, fg_color=palette.window_bg, corner_radius=0)
            self._frames[name] = frame

        self._build_welcome(self._frames["Welcome"])
        self._build_cli(self._frames["arduino-cli"])
        self._build_folders(self._frames["Folders"])
        self._build_board(self._frames["Board & port"])

        footer = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(1, weight=1)
        self.progress = ctk.CTkProgressBar(footer, height=6, progress_color=palette.accent,
                                            fg_color=palette.surface)
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.progress.set(0.0)
        self.back_button = ctk.CTkButton(footer, text="Back", width=90, height=32, corner_radius=8,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text, command=self._go_back)
        self.back_button.grid(row=1, column=0, sticky="w", padx=16, pady=12)
        self.status_label = ctk.CTkLabel(footer, text="", font=(palette.font_family, 10),
                                         text_color=palette.text_dim, anchor="w", justify="left", wraplength=330)
        self.status_label.grid(row=1, column=1, sticky="ew", padx=10)
        self.skip_button = ctk.CTkButton(footer, text="Skip step", width=96, height=32, corner_radius=8,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text_dim, command=self._go_next)
        self.skip_button.grid(row=1, column=2, sticky="e", padx=6)
        self.next_button = ctk.CTkButton(footer, text="Next", width=110, height=32, corner_radius=8,
                                         fg_color=palette.accent, hover_color=palette.accent_hover,
                                         text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                         command=self._go_next)
        self.next_button.grid(row=1, column=3, sticky="e", padx=(6, 16), pady=12)

    # ------------------------------------------------------------ step layout
    def _build_welcome(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        box = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=10)
        box.grid(row=0, column=0, sticky="nsew", padx=18, pady=14)
        box.grid_columnconfigure(0, weight=1)
        lines = (
            ("Arduino Studio compiles and flashes Arduino sketches with the official Arduino CLI.", "head"),
            ("", ""),
            ("You do not need the Arduino IDE open - but it does need to be installed once, or a standalone", "text"),
            ("arduino-cli download, so that the board platforms (AVR, ESP32, ESP8266) are available.", "text"),
            ("", ""),
            ("This wizard will ask for:", "head"),
            ("   1.  where arduino-cli lives (auto-detect usually finds it)", "text"),
            ("   2.  the folders that hold cores, libraries and sketches", "text"),
            ("   3.  which board you want to start with", "text"),
            ("", ""),
            ("Everything here can be changed later in Settings. Nothing is downloaded without asking.", "dim"),
            ("", ""),
            ("Download page for the CLI:  https://docs.arduino.cc/arduino-cli/", "link"),
        )
        row = 0
        for text, tone in lines:
            color = {"head": palette.text, "text": palette.text_dim, "dim": palette.text_muted,
                     "link": palette.info}.get(tone, palette.text_dim)
            label = ctk.CTkLabel(box, text=text, font=(palette.font_family, 12 if tone == "head" else 11),
                                 text_color=color, anchor="w", justify="left", wraplength=600)
            label.grid(row=row, column=0, sticky="w", padx=18, pady=(2, 2))
            row += 1

    def _build_cli(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="arduino-cli path", font=(palette.font_family, 11), text_color=palette.text,
                     anchor="w").grid(row=0, column=0, sticky="w", padx=(18, 10), pady=(16, 2))
        self.path_entry = ctk.CTkEntry(parent, height=30, font=(palette.mono_family, 11), fg_color=palette.surface,
                                       text_color=palette.text, border_color=palette.border,
                                       placeholder_text=r"C:\Users\you\AppData\Local\Arduino15\arduino-cli.exe")
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(16, 2))
        ctk.CTkButton(parent, text="Browse\u2026", width=90, height=30, corner_radius=6, fg_color=palette.surface,
                      hover_color=palette.hover, text_color=palette.text, command=self._browse_cli)\
            .grid(row=0, column=2, sticky="e", padx=(0, 18), pady=(16, 2))

        self.auto_check = ctk.CTkCheckBox(parent, text="search PATH and the usual install folders automatically",
                                          font=(palette.font_family, 10), text_color=palette.text_dim,
                                          checkbox_width=16, checkbox_height=16, command=self._toggle_auto)
        self.auto_check.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(2, 8))

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", padx=18, pady=(4, 6))
        self.detect_button = ctk.CTkButton(actions, text="Detect now", width=120, height=30, corner_radius=8,
                                           fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                                           command=self.probe_cli)
        self.detect_button.pack(side="left", padx=(0, 8))
        self.test_button = ctk.CTkButton(actions, text="Test connection", width=140, height=30, corner_radius=8,
                                         fg_color=palette.accent, hover_color=palette.accent_hover,
                                         text_color=palette.accent_text, command=self.probe_cli)
        self.test_button.pack(side="left")
        self.cli_result = ctk.CTkLabel(actions, text="not tested yet", font=(palette.font_family, 10),
                                       text_color=palette.text_muted, anchor="w", justify="left", wraplength=280)
        self.cli_result.pack(side="left", padx=(12, 0))

        self.config_frame = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        self.config_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=18, pady=(8, 16))
        self.config_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.config_frame, text="extra arguments", font=(palette.font_family, 10),
                     text_color=palette.text_dim, anchor="w").grid(row=0, column=0, sticky="w", padx=(12, 8), pady=8)
        self.extra_entry = ctk.CTkEntry(self.config_frame, height=26, font=(palette.mono_family, 10),
                                       fg_color=palette.surface, text_color=palette.text,
                                       border_color=palette.border, placeholder_text="--additional-urls")
        self.extra_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=8)
        ctk.CTkLabel(self.config_frame,
                     text="Use this if arduino-cli needs a specific config file or proxy settings; leave empty otherwise.",
                     font=(palette.font_family, 9), text_color=palette.text_muted, anchor="w", justify="left",
                     wraplength=620).grid(row=1, column=0, columnspan=3, sticky="w", padx=(12, 12), pady=(0, 8))

    def _build_folders(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_columnconfigure(1, weight=1)
        self._data_entry = self._path_row(parent, 0, "cores / libraries (directories.data)", "arduino_data_dir")
        self._sketch_entry = self._path_row(parent, 1, "sketchbook (directories.user)", "sketchbook_dir")

        urls = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        urls.grid(row=2, column=0, columnspan=3, sticky="ew", padx=18, pady=(10, 6))
        urls.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(urls, text="Additional boards (optional tick to add the manager URL)",
                     font=(palette.font_family, 10, "bold"), text_color=palette.text_dim, anchor="w")\
            .grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        self._url_checks: dict[str, ctk.CTkCheckBox] = {}
        row = 1
        for name, url in BOARD_MANAGER_URLS.items():
            check = ctk.CTkCheckBox(urls, text=f"{name}   ({url})", font=(palette.font_family, 10),
                                    text_color=palette.text, checkbox_width=16, checkbox_height=16)
            check.grid(row=row, column=0, sticky="w", padx=14, pady=3)
            self._url_checks[url] = check
            row += 1
        ctk.CTkLabel(parent,
                     text="Leave the folders empty to use arduino-cli's own defaults (recommended).",
                     font=(palette.font_family, 9), text_color=palette.text_muted, anchor="w", justify="left",
                     wraplength=640).grid(row=row, column=0, columnspan=3, sticky="w", padx=(18, 18), pady=(6, 16))

    def _build_board(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="Board", font=(palette.font_family, 11), text_color=palette.text, anchor="w")\
            .grid(row=0, column=0, sticky="w", padx=(18, 10), pady=(16, 2))
        self.board_menu = ctk.CTkOptionMenu(parent, values=["Arduino Uno"], height=30, width=320,
                                            font=(palette.font_family, 11), command=lambda value: self._board_picked(value))
        self.board_menu.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(16, 2))
        self.refresh_board_button = ctk.CTkButton(parent, text="From CLI", width=100, height=30, corner_radius=6,
                                                  fg_color=palette.surface, hover_color=palette.hover,
                                                  text_color=palette.text, command=self.load_boards)
        self.refresh_board_button.grid(row=0, column=2, sticky="e", padx=(0, 18), pady=(16, 2))

        ctk.CTkLabel(parent, text="Port", font=(palette.font_family, 11), text_color=palette.text, anchor="w")\
            .grid(row=1, column=0, sticky="w", padx=(18, 10), pady=4)
        self.port_menu = ctk.CTkOptionMenu(parent, values=["No port detected"], height=30, width=320,
                                           font=(palette.font_family, 11))
        self.port_menu.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)
        ctk.CTkButton(parent, text="Rescan", width=100, height=30, corner_radius=6, fg_color=palette.surface,
                      hover_color=palette.hover, text_color=palette.text, command=self.load_ports)\
            .grid(row=1, column=2, sticky="e", padx=(0, 18), pady=4)

        ctk.CTkLabel(parent, text="Custom FQBN", font=(palette.font_family, 11), text_color=palette.text,
                     anchor="w").grid(row=2, column=0, sticky="w", padx=(18, 10), pady=4)
        self.fqbn_entry = ctk.CTkEntry(parent, height=30, font=(palette.mono_family, 11), fg_color=palette.surface,
                                       text_color=palette.text, border_color=palette.border,
                                       placeholder_text="arduino:avr:uno (optional, overrides the picker)")
        self.fqbn_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 18), pady=4)

        self.board_note = ctk.CTkLabel(parent, text="", font=(palette.font_family, 10), text_color=palette.text_muted,
                                       anchor="w", justify="left", wraplength=640)
        self.board_note.grid(row=3, column=0, columnspan=3, sticky="w", padx=(18, 18), pady=(6, 4))

        self.create_project_check = ctk.CTkCheckBox(parent, text="create a blank sketch for this board on startup",
                                                     font=(palette.font_family, 10), text_color=palette.text_dim,
                                                     checkbox_width=16, checkbox_height=16)
        self.create_project_check.grid(row=4, column=0, columnspan=3, sticky="w", padx=(18, 10), pady=(6, 16))

    def _path_row(self, parent: Any, row: int, label: str, key: str) -> ctk.CTkEntry:
        palette = self.palette
        ctk.CTkLabel(parent, text=label, font=(palette.font_family, 11), text_color=palette.text, anchor="w")\
            .grid(row=row, column=0, sticky="w", padx=(18, 10), pady=6)
        entry = ctk.CTkEntry(parent, height=28, font=(palette.mono_family, 10), fg_color=palette.surface,
                             text_color=palette.text, border_color=palette.border,
                             placeholder_text="(arduino-cli default)")
        entry.grid(row=row, column=1, sticky="ew", padx=(0, 6), pady=6)
        ctk.CTkButton(parent, text="Browse\u2026", width=90, height=28, corner_radius=6, fg_color=palette.surface,
                      hover_color=palette.hover, text_color=palette.text,
                      command=lambda: self._browse_dir(entry, label)).grid(row=row, column=2, sticky="e",
                                                                            padx=(0, 18), pady=6)
        return entry

    # ------------------------------------------------------------------ steps
    def _show_step(self, index: int) -> None:
        self._step = max(0, min(len(_STEPS) - 1, index))
        for frame in self._frames.values():
            frame.grid_forget()
        name = _STEPS[self._step]
        frame = self._frames[name]
        frame.grid(row=0, column=0, sticky="nsew")
        titles = {
            "Welcome": ("Welcome to Arduino Studio",
                        "A standalone Arduino IDE that drives the official Arduino CLI. Let's point it at your install."),
            "arduino-cli": ("Step 1 of 3 - locate arduino-cli",
                            "The app only talks to arduino-cli, so this is the one thing that must be right."),
            "Folders": ("Step 2 of 3 - folders",
                        "Where cores and libraries are installed, and where arduino-cli keeps its sketchbook."),
            "Board & port": ("Step 3 of 3 - board and port",
                             "Pick the board you will use; the port list comes from the CLI."),
        }
        title, subtitle = titles[name]
        try:
            self.step_label.configure(text=title)
            self.sub_label.configure(text=subtitle)
            self.progress.set((self._step) / float(len(_STEPS) - 1))
            self.back_button.configure(state="normal" if self._step > 0 else "disabled")
            self.skip_button.configure(state="normal" if self._step > 0 else "disabled")
            self.next_button.configure(text="Finish" if self._step == len(_STEPS) - 1 else "Next")
        except tk.TclError:  # pragma: no cover
            pass
        if name == "Board & port" and not self._boards:
            self.load_boards()
            self.load_ports()

    def _go_next(self) -> None:
        if self._step == 1 and not self._validate_cli():
            return
        if self._step == len(_STEPS) - 1:
            self._finish()
            return
        if self._step == 0:
            self._prefill()
        self._show_step(self._step + 1)

    def _go_back(self) -> None:
        if self._step == 0:
            self._on_cancel()
            return
        self._show_step(self._step - 1)

    def _prefill(self) -> None:
        """Fill the CLI step from settings / detection results."""
        try:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, self._settings.arduino_cli_path or "")
            self.extra_entry.delete(0, "end")
            self.extra_entry.insert(0, self._settings.cli_extra_args or "")
            self._data_entry.delete(0, "end")
            self._data_entry.insert(0, self._settings.arduino_data_dir or "")
            self._sketch_entry.delete(0, "end")
            self._sketch_entry.insert(0, self._settings.sketchbook_dir or "")
            self.auto_check.select() if self._settings.auto_detect_cli else self.auto_check.deselect()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _toggle_auto(self) -> None:
        try:
            enabled = bool(self.auto_check.get())
        except (tk.TclError, AttributeError):  # pragma: no cover
            return
        if enabled and not str(self.path_entry.get()).strip():
            self.probe_cli()

    # ------------------------------------------------------------------- cli
    def probe_cli(self) -> None:
        """Detect or test ``arduino-cli`` (in the query lane when possible)."""
        path = ""
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        self._set_result("testing arduino-cli\u2026", None)
        if self._runner is not None and hasattr(self._runner, "submit"):
            chosen_path = path

            def work(context: Any) -> CLIInfo:
                cli = ArduinoCLI(cli_path=chosen_path)
                target = chosen_path or cli.resolve("")     # PATH + the usual install folders
                return cli.probe(target)

            def done(result: Any) -> None:
                info = getattr(result, "payload", None)
                self._apply_probe(info)

            self._runner.submit("wizard:cli-probe", work, on_done=done)
            return
        cli = ArduinoCLI(cli_path=path)
        self._apply_probe(cli.probe(path))

    def _apply_probe(self, info: Optional[CLIInfo]) -> None:
        self._cli_info = info
        if info is None or not info.ok:
            message = (info.message if info is not None else "arduino-cli did not answer") or "arduino-cli not found"
            self._set_result(message, False)
            return
        self._set_result(f"arduino-cli {info.version} - {info.path}", True)
        try:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, info.path)
        except tk.TclError:  # pragma: no cover
            pass
        if info.data_dir and not str(self._data_entry.get()).strip():
            try:
                self._data_entry.delete(0, "end")
                self._data_entry.insert(0, str(info.data_dir))
            except tk.TclError:  # pragma: no cover
                pass

    def _set_result(self, text: str, ok: Optional[bool]) -> None:
        color = self.palette.text_muted if ok is None else (self.palette.success if ok else self.palette.error)
        try:
            self.cli_result.configure(text=text, text_color=color)
            self.status_label.configure(text=text if ok is False else "",
                                       text_color=self.palette.error if ok is False else self.palette.text_dim)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _validate_cli(self) -> bool:
        """The CLI step must end with a working path (or an explicit skip)."""
        info = self._cli_info
        if info is not None and info.ok:
            return True
        path = ""
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        if path and Path(path).is_file():
            return True
        answer = ask_message(
            self, kind="warning", title="arduino-cli not verified",
            message="The path does not point at a working arduino-cli.",
            detail="Without it nothing can be compiled or uploaded. You can still continue and fix the path "
                   "later in Settings, but Verify / Upload will report the problem until you do.",
            buttons=("Go back", "Continue anyway"), palette=self.palette,
        )
        return answer == "Continue anyway"

    # ------------------------------------------------------------ boards/ports
    def load_boards(self) -> None:
        """Ask the CLI for its board list (falls back to the built-in profiles)."""
        fallback = sort_board_labels([profile.name for profile in BUILTIN_BOARDS])
        self._boards = list(fallback)
        path = ""
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        cli = ArduinoCLI(cli_path=path)
        try:
            boards = cli.board_listall()
        except Exception as exc:  # CLIError and friends - the fallback is fine here
            self._log.info("board listall failed in the wizard: %s", exc)
            boards = []
        if boards:
            self._boards = sort_board_labels([board.name or board.fqbn for board in boards])
        try:
            self.board_menu.configure(values=self._boards or ["Arduino Uno"])
            wanted = display_name_for_fqbn(self._settings.fqbn) or "Arduino Uno"
            self.board_menu.set(wanted if wanted in self._boards else (self._boards[0] if self._boards else "Arduino Uno"))
            self.board_note.configure(text=f"{len(self._boards)} boards available. "
                                           f"FQBN: {self._fqbn_for_label(self.board_menu.get())}")
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def load_ports(self) -> None:
        """Read the detected serial ports (never blocks longer than the CLI timeout)."""
        path = ""
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        cli = ArduinoCLI(cli_path=path)
        try:
            ports = cli.board_list()
        except Exception as exc:  # pragma: no cover - CLIError
            self._log.info("board list failed in the wizard: %s", exc)
            ports = []
        self._ports = [port.label or port.address for port in ports]
        try:
            self.port_menu.configure(values=self._ports or ["No port detected"])
            if not self._ports:
                self.port_menu.set("No port detected")
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _board_picked(self, label: Any) -> None:
        try:
            self.board_note.configure(text=f"FQBN: {self._fqbn_for_label(str(label))}")
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _fqbn_for_label(self, label: str) -> str:
        for profile in BUILTIN_BOARDS:
            if profile.name == label:
                return profile.fqbn
        for board in self._board_objects():
            if board.name == label:
                return board.fqbn
        return label or "arduino:avr:uno"

    def _board_objects(self) -> list[Any]:
        path = ""
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        cached = getattr(self, "_board_cache", None)
        if cached:
            return cached
        try:
            boards = ArduinoCLI(cli_path=path).board_listall()
        except Exception:  # pragma: no cover
            boards = []
        self._board_cache = list(boards)
        return self._board_cache

    # ------------------------------------------------------------------ misc
    def _browse_cli(self) -> None:
        from tkinter import filedialog

        try:
            chosen = filedialog.askopenfilename(
                parent=self, title="Locate arduino-cli",
                filetypes=[("arduino-cli", "arduino-cli*"), ("Executables", "*.exe *.bat *.cmd"),
                           ("All files", "*.*")],
            )
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen:
            return
        try:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, str(Path(chosen)))
        except tk.TclError:  # pragma: no cover
            return
        self.probe_cli()

    def _browse_dir(self, entry: ctk.CTkEntry, title: str) -> None:
        from tkinter import filedialog

        try:
            initial = str(entry.get()).strip() or str(Path.home())
        except tk.TclError:  # pragma: no cover
            initial = str(Path.home())
        try:
            chosen = filedialog.askdirectory(parent=self, title=title, initialdir=initial)
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen:
            return
        try:
            entry.delete(0, "end")
            entry.insert(0, str(Path(chosen)))
        except tk.TclError:  # pragma: no cover
            pass

    # --------------------------------------------------------------- results
    def apply_choices(self) -> None:
        """Write the wizard's answers into the settings (without closing)."""
        changes: dict[str, Any] = {}
        try:
            path = str(self.path_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            path = ""
        if path:
            changes["arduino_cli_path"] = path
        try:
            changes["auto_detect_cli"] = bool(self.auto_check.get())
            changes["cli_extra_args"] = str(self.extra_entry.get()).strip()
            changes["arduino_data_dir"] = str(self._data_entry.get()).strip()
            changes["sketchbook_dir"] = str(self._sketch_entry.get()).strip()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        urls = [url for url, check in self._url_checks.items() if _checked(check)]
        if urls:
            changes["additional_board_urls"] = list(dict.fromkeys(list(self._settings.additional_board_urls) + urls))
        fqbn = self._fqbn_for_label(str(_safe_get(self.board_menu)))
        custom = ""
        try:
            custom = str(self.fqbn_entry.get()).strip()
        except tk.TclError:  # pragma: no cover
            custom = ""
        if custom:
            changes["use_custom_fqbn"] = True
            changes["custom_fqbn"] = custom
            fqbn = custom
        changes["fqbn"] = fqbn
        port = str(_safe_get(self.port_menu) or "")
        if port and not port.startswith("No port"):
            changes["port"] = port.split(" - ")[0].strip()
        changes["first_run_completed"] = True
        self._settings.update(**changes)
        try:
            self._store.save()
        except Exception:  # pragma: no cover
            self._log.exception("the wizard could not write settings.json")
        if self._on_board_picked is not None and fqbn:
            self._on_board_picked(fqbn)

    def create_blank_project(self) -> bool:
        """True when the user asked for a starter sketch."""
        return _checked(getattr(self, "create_project_check", None))

    def _finish(self) -> None:
        self.apply_choices()
        self._finished = True
        self._close()

    def _on_cancel(self) -> None:
        # closing the window is allowed: the settings keep whatever was valid
        self.apply_choices()
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:  # pragma: no cover
            pass
        self.destroy()

    def run(self) -> bool:
        """Show the wizard modally and return whether it completed."""
        try:
            self.wait_window()
        except tk.TclError:  # pragma: no cover - headless / stub
            return self._finished
        return self._finished


def _checked(widget: Any) -> bool:
    if widget is None:
        return False
    try:
        return bool(widget.get())
    except (tk.TclError, AttributeError):  # pragma: no cover
        return False


def _safe_get(widget: Any) -> str:
    if widget is None:
        return ""
    try:
        return str(widget.get())
    except (tk.TclError, AttributeError):  # pragma: no cover
        return ""


def run_first_run_wizard(master: Any, palette: Palette, *, store: SettingsStore, settings: Settings,
                        runner: Any = None, on_board_picked: Optional[Callable[[str], Any]] = None) -> bool:
    """Build and run :class:`SetupWizard`; ``False`` when the user cancelled."""
    wizard = SetupWizard(master, palette, store=store, settings=settings, runner=runner,
                        on_board_picked=on_board_picked)
    try:
        return wizard.run()
    except Exception:  # pragma: no cover - a broken wizard must not stop the app
        get_logger("ui.wizard").exception("first-run wizard failed")
        return False
