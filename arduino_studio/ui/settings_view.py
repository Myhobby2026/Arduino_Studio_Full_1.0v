"""Application settings screen.

Everything the user may want to tune lives in ``settings.json`` (see
:mod:`arduino_studio.core.settings`); this view edits that same live object, so
changes take effect immediately and are written out when the app closes (or when
*Save now* is pressed).

The form is generated from :data:`SETTINGS_TABS` - a declarative list of
:class:`FieldSpec` rows - so a new setting only needs one line here plus its
default in the dataclass.  Widgets write through :meth:`Settings.update`, which
routes to the owning :class:`SettingsStore` and notifies listeners, so the app
can re-theme, re-font or restart the terminal from a single callback.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.boards import BOARD_MANAGER_URLS
from ..core.settings import COMMON_BAUD_RATES, LINE_ENDINGS, Settings, SettingsStore, coerce_string_list
from ..core.utils import Debouncer, get_logger
from .theme import Palette
from .widgets.dialogs import ask_choice, ask_yes_no

__all__ = ["SettingsView", "SETTINGS_TABS"]

# Combo boxes store a machine value but show a friendly label.
_WARN_LEVELS = (("none", "none (quiet)"), ("default", "default"), ("all", "all (verbose)"),
                ("inhibit", "inhibit (none at all)"))
_BUILD_OUTPUT = (("project", "in the project's build folder"), ("temp", "in a temporary folder"))
_APPEARANCE = (("Dark", "Dark"), ("Light", "Light"), ("System", "Match Windows"))
_LINE_ENDINGS = tuple(
    (code, {"None": "No line ending", "LF": "Newline (LF)", "CR": "Carriage return (CR)",
            "CRLF": "Both NL & CR (CRLF)"}[code])
    for code in LINE_ENDINGS
)
_SHELLS = (("powershell", "PowerShell"), ("cmd", "Command Prompt (cmd)"))
_LIMITS = {
    "tab_size": (1, 16), "editor_font_size": (6, 40), "ui_font_size": (7, 22),
    "console_font_size": (6, 32), "serial_font_size": (6, 32), "terminal_font_size": (6, 32),
    "console_max_lines": (200, 200000), "max_recent_projects": (1, 50),
    "autosave_interval_sec": (0, 3600), "lib_index_max_age_days": (0, 365),
}


@dataclass(frozen=True)
class FieldSpec:
    """One row of the settings form."""

    key: str
    kind: str                                # check | entry | int | combo | path | list
    label: str
    hint: str = ""
    options: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()   # (stored value, shown label)
    mono: bool = False
    browse: str = ""                          # "" | "dir" | "file"
    width: int = 260


SETTINGS_TABS: dict[str, tuple[FieldSpec, ...]] = {
    "Editor": (
        FieldSpec("editor_font_family", "combo", "Font",
                  options=("Consolas", "Cascadia Code", "Cascadia Mono", "Courier New", "Segoe UI Mono",
                           "JetBrains Mono", "Monospace"), hint="monospaced fonts work best"),
        FieldSpec("editor_font_size", "int", "Font size", hint="points - Ctrl+wheel zooms inside the editor"),
        FieldSpec("tab_size", "int", "Tab width", hint="spaces inserted by one Tab press"),
        FieldSpec("insert_spaces", "check", "Insert spaces instead of tabs"),
        FieldSpec("auto_indent", "check", "Auto-indent new lines"),
        FieldSpec("auto_close_brackets", "check", "Auto-close brackets and braces"),
        FieldSpec("auto_close_quotes", "check", "Auto-close quotes"),
        FieldSpec("auto_complete", "check", "Word auto-completion", hint="offers identifiers from the open file"),
        FieldSpec("highlight_current_line", "check", "Highlight the current line"),
        FieldSpec("show_line_numbers", "check", "Show the line-number gutter"),
        FieldSpec("show_minimap_gutter", "check", "Mark diagnostic lines in the gutter"),
        FieldSpec("word_wrap", "check", "Soft word wrap"),
        FieldSpec("trim_trailing_ws_on_save", "check", "Trim trailing whitespace on save"),
        FieldSpec("ensure_final_newline_on_save", "check", "Ensure the file ends with a newline"),
        FieldSpec("autosave_interval_sec", "int", "Auto-save after (seconds)", hint="0 disables auto-save"),
    ),
    "Appearance": (
        FieldSpec("appearance_mode", "combo", "Theme", pairs=_APPEARANCE, hint="re-colours every panel instantly"),
        FieldSpec("accent_color", "combo", "Accent colour",
                  options=("#2f81f7", "#0f9d58", "#e8710a", "#d13438", "#7a5af5", "#00b7c3"),
                  hint="used for buttons, selection and the active tab"),
        FieldSpec("ui_font_family", "combo", "Interface font",
                  options=("Segoe UI", "TkDefaultFont", "Arial", "Helvetica Neue", "Roboto")),
        FieldSpec("ui_font_size", "int", "Interface font size", hint="labels, menus and panels"),
        FieldSpec("console_font_size", "int", "Console font size"),
        FieldSpec("serial_font_size", "int", "Serial monitor font size"),
        FieldSpec("terminal_font_size", "int", "Terminal font size"),
        FieldSpec("console_max_lines", "int", "Console scroll-back (lines)",
                  hint="older output is dropped past this limit"),
    ),
    "Arduino CLI": (
        FieldSpec("arduino_cli_path", "path", "arduino-cli path", browse="file", width=420,
                  hint="leave empty to search PATH and the usual install folders"),
        FieldSpec("auto_detect_cli", "check", "Search for arduino-cli automatically"),
        FieldSpec("cli_config_file", "path", "Custom --config-file", browse="file", width=420,
                  hint="optional; use it if you share a profile with the Arduino IDE"),
        FieldSpec("cli_extra_args", "entry", "Extra global arguments", mono=True, width=420,
                  hint="appended to every command, e.g. --additional-urls"),
        FieldSpec("arduino_data_dir", "path", "Data directory", browse="dir", width=420,
                  hint="directories.data - where cores and libraries are installed"),
        FieldSpec("sketchbook_dir", "path", "Sketchbook", browse="dir", width=420,
                  hint="directories.user - arduino-cli's own sketch folder"),
        FieldSpec("verbose_cli_output", "check", "Verbose compiler output (-v)"),
        FieldSpec("compile_warnings", "combo", "Compiler warnings", pairs=_WARN_LEVELS),
        FieldSpec("clean_build", "check", "Always clean before verifying",
                  hint="slower, but rules out stale build-cache surprises"),
        FieldSpec("build_output_mode", "combo", "Build folder", pairs=_BUILD_OUTPUT),
        FieldSpec("install_missing_cores", "check", "Offer to install a missing board platform"),
        FieldSpec("check_missing_includes", "check", "Offer to install libraries for unknown #include"),
        FieldSpec("console_timestamps", "check", "Stamp console lines with the time"),
    ),
    "Boards & Libraries": (
        FieldSpec("use_custom_fqbn", "check", "Use a custom board FQBN",
                  hint="ignore the board picker and always compile with the FQBN below"),
        FieldSpec("custom_fqbn", "entry", "Custom FQBN", mono=True, width=420,
                  hint="e.g. arduino:avr:nano:cpu=atmega328old"),
        FieldSpec("additional_board_urls", "list", "Additional board manager URLs", width=520,
                  hint="one URL per line; they are configured with 'arduino-cli config add' when needed"),
        FieldSpec("lib_auto_update_index", "check", "Refresh the library index automatically when it is old"),
        FieldSpec("lib_index_max_age_days", "int", "Index age considered old (days)"),
        FieldSpec("lib_use_project_dir", "check", "Install new libraries into the project's libraries folder",
                  hint="keeps the sketchbook clean; the folder travels with the project"),
        FieldSpec("max_recent_projects", "int", "Recent projects to remember"),
    ),
    "Serial monitor": (
        FieldSpec("serial_baud", "combo", "Default baud rate",
                  options=tuple(str(value) for value in COMMON_BAUD_RATES)),
        FieldSpec("serial_line_ending", "combo", "Sent line ending", pairs=_LINE_ENDINGS),
        FieldSpec("serial_timestamps", "check", "Show timestamps"),
        FieldSpec("serial_show_rx", "check", "Show received characters"),
        FieldSpec("serial_show_tx", "check", "Echo what is sent"),
        FieldSpec("serial_autoscroll", "check", "Auto-scroll to the bottom"),
        FieldSpec("serial_hex_display", "check", "Show incoming bytes as hex"),
        FieldSpec("serial_toggle_dtr", "check", "Toggle DTR when opening (resets most boards)"),
        FieldSpec("serial_toggle_rts", "check", "Toggle RTS when opening"),
        FieldSpec("serial_auto_reopen_after_upload", "check", "Re-open the port after a successful upload",
                  hint="the monitor releases the port before uploading so avrdude/esptool can use it"),
        FieldSpec("serial_log_dir", "path", "Default folder for saved logs", browse="dir", width=420),
    ),
    "Terminal": (
        FieldSpec("terminal_shell", "combo", "Shell", pairs=_SHELLS,
                  hint="the Integrated Terminal tab starts this shell in the project folder"),
        FieldSpec("terminal_follow_project_dir", "check", "Restart in the project folder when it changes"),
        FieldSpec("terminal_confirm_destructive", "check", "Ask before running destructive commands",
                  hint="rm -rf, format, diskpart, Remove-Item -Recurse, git push --force and friends"),
    ),
    "Bootloader": (
        FieldSpec("bootloader_programmer", "entry", "Default programmer id", mono=True, width=300,
                  hint="arduino, usbasp, mkII (Atmel-ICE/AVRISP), usbtinyisp, ..."),
        FieldSpec("bootloader_last_chip", "entry", "Default target chip", mono=True, width=300),
        FieldSpec("bootloader_last_clock", "entry", "Default clock / fuse profile", width=300,
                  hint="must match one of the profiles listed on the Bootloader tab"),
        FieldSpec("bootloader_backup_dir", "path", "Folder for firmware backups", browse="dir", width=420),
        FieldSpec("bootloader_confirm_required", "check", "Always confirm before writing fuses or bootloaders",
                  hint="leave enabled: wrong fuse bytes can make a chip unreachable over ISP"),
    ),
}


class SettingsView(ctk.CTkFrame):
    """Tabbed settings form bound to the live :class:`Settings` object."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        store: Optional[SettingsStore] = None,
        settings: Optional[Settings] = None,
        on_change: Optional[Callable[[str, Any], Any]] = None,
        on_detect_cli: Optional[Callable[[], Any]] = None,
        on_test_cli: Optional[Callable[[], Any]] = None,
        on_close: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self._store = store
        self._settings = settings if settings is not None else (store.settings if store is not None else Settings())
        self._on_change = on_change
        self._on_detect_cli = on_detect_cli
        self._on_test_cli = on_test_cli
        self._on_close = on_close
        self._log = get_logger("ui.settings")
        self.palette = palette
        self._widgets: dict[str, Any] = {}
        self._entries: dict[str, Any] = {}
        self._boxes: dict[str, Any] = {}
        self._checks: dict[str, Any] = {}
        self._combos: dict[str, Any] = {}
        self._readers: dict[str, Callable[[], Any]] = {}
        self._suppress = False

        self._build()
        self.reload()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        header.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(header, text="Settings", font=(palette.font_family, 13, "bold"),
                                        text_color=palette.text, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w", padx=14, pady=(8, 0))
        self.hint_label = ctk.CTkLabel(
            header, text="Changes apply as you edit them and are saved to settings.json when the app closes.",
            font=(palette.font_family, 10), text_color=palette.text_muted, anchor="w", justify="left",
            wraplength=720,
        )
        self.hint_label.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 8))
        self.save_button = ctk.CTkButton(header, text="Save now", width=96, height=28, corner_radius=6,
                                         fg_color=palette.accent, hover_color=palette.accent_hover,
                                         text_color=palette.accent_text, command=self.save_now)
        self.save_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 12), pady=8)

        self.tabs = ctk.CTkTabview(self, fg_color=palette.window_bg, corner_radius=8,
                                   segmented_button_fg_color=palette.panel_bg,
                                   segmented_button_selected_color=palette.accent,
                                   segmented_button_unselected_color=palette.panel_bg,
                                   segmented_button_selected_hover_color=palette.accent_hover,
                                   text_color=palette.text)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        for name in SETTINGS_TABS:
            self.tabs.add(name)
        self.tabs.set("Editor")
        for name, specs in SETTINGS_TABS.items():
            self._build_tab(self.tabs.tab(name), name, specs)

        footer = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8)
        footer.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        footer.grid_columnconfigure(1, weight=1)
        self.reset_button = ctk.CTkButton(footer, text="Restore this tab", width=140, height=28, corner_radius=6,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text,
                                          command=self.reset_current_tab)
        self.reset_button.grid(row=0, column=0, sticky="w", padx=12, pady=8)
        self.status_label = ctk.CTkLabel(footer, text="", font=(palette.font_family, 10),
                                         text_color=palette.text_dim, anchor="w")
        self.status_label.grid(row=0, column=1, sticky="ew", padx=10)
        self.close_button = ctk.CTkButton(footer, text="Close", width=96, height=28, corner_radius=6,
                                          fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                                          command=self._close)
        self.close_button.grid(row=0, column=2, sticky="e", padx=12, pady=8)

    def _build_tab(self, parent: Any, name: str, specs: tuple[FieldSpec, ...]) -> None:
        """Populate one tab with a scrollable form (each field uses two rows)."""
        palette = self.palette
        if parent is None:  # pragma: no cover - defensive with odd toolkits
            return
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(parent, fg_color=palette.window_bg, corner_radius=0)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=0, minsize=220)
        body.grid_columnconfigure(1, weight=1)
        for index, spec in enumerate(specs):
            self._build_field(body, index * 2, spec)
        extra_row = len(specs) * 2
        if name == "Arduino CLI":
            self._build_cli_row(body, extra_row)
        elif name == "Boards & Libraries":
            self._build_url_row(body, extra_row)

    def _build_field(self, parent: Any, row: int, spec: FieldSpec) -> None:
        palette = self.palette
        ctk.CTkLabel(parent, text=spec.label, font=(palette.font_family, 10), text_color=palette.text,
                     anchor="w", justify="left").grid(row=row, column=0, sticky="w", padx=(14, 10), pady=(8, 2))
        widget = self._make_widget(parent, spec)
        if widget is not None:
            widget.grid(row=row, column=1, sticky="ew" if spec.kind in ("entry", "path", "combo", "int") else "w",
                        padx=(0, 14), pady=(8, 2))
            self._widgets[spec.key] = widget
        if spec.hint:
            ctk.CTkLabel(parent, text=spec.hint, font=(palette.font_family, 9), text_color=palette.text_muted,
                         anchor="w", justify="left", wraplength=560)\
                .grid(row=row + 1, column=1, sticky="w", padx=(0, 14), pady=(0, 6))

    def _make_widget(self, parent: Any, spec: FieldSpec) -> Optional[Any]:
        """Create the editor for *spec* and wire it to the settings object."""
        palette = self.palette
        if spec.kind == "check":
            widget = ctk.CTkCheckBox(parent, text="", width=20, height=22, checkbox_width=18, checkbox_height=18,
                                     fg_color=palette.accent, hover_color=palette.accent_hover,
                                     border_color=palette.border, command=lambda: self._commit(spec))
            self._checks[spec.key] = widget
            self._readers[spec.key] = widget.get
            return widget
        if spec.kind in ("entry", "path", "int"):
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            frame.grid_columnconfigure(0, weight=1)
            entry = ctk.CTkEntry(frame, height=28, width=spec.width,
                                 font=(palette.mono_family, 10) if spec.mono else (palette.font_family, 10),
                                 fg_color=palette.surface, text_color=palette.text, border_color=palette.border,
                                 placeholder_text="number" if spec.kind == "int" else "")
            entry.grid(row=0, column=0, sticky="ew")
            self._entries[spec.key] = entry
            if spec.kind == "path":
                ctk.CTkButton(frame, text="Browse\u2026", width=84, height=28, corner_radius=6,
                              fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                              command=lambda: self._browse(spec))\
                    .grid(row=0, column=1, sticky="e", padx=(6, 0))
                ctk.CTkButton(frame, text="Clear", width=62, height=28, corner_radius=6, fg_color="transparent",
                              border_width=1, border_color=palette.border, hover_color=palette.hover,
                              text_color=palette.text_muted, command=lambda: self._clear(spec))\
                    .grid(row=0, column=2, sticky="e", padx=(6, 0))
            debouncer = Debouncer(450, lambda: self._commit(spec), scheduler=self.after,
                                  canceller=self.after_cancel)
            entry.bind("<KeyRelease>", lambda event: debouncer.hit(), add=True)
            entry.bind("<FocusOut>", lambda event: self._commit(spec), add=True)
            entry.bind("<Return>", lambda event: self._commit(spec, enter=True), add=True)
            self._readers[spec.key] = entry.get
            return frame
        if spec.kind == "combo":
            labels = [label for _, label in spec.pairs] if spec.pairs else list(spec.options)
            widget = ctk.CTkOptionMenu(parent, values=labels or ["-"], width=spec.width, height=28,
                                       dropdown_font=(palette.font_family, 10),
                                       command=lambda value, spec=spec: self._commit(spec, value))
            self._combos[spec.key] = widget
            self._readers[spec.key] = widget.get
            return widget
        if spec.kind == "list":
            box = ctk.CTkTextbox(parent, height=96, width=spec.width, wrap="word", fg_color=palette.surface,
                                 text_color=palette.text, font=(palette.mono_family, 10))
            debouncer = Debouncer(700, lambda: self._commit(spec), scheduler=self.after,
                                  canceller=self.after_cancel)
            box.bind("<KeyRelease>", lambda event: debouncer.hit(), add=True)
            box.bind("<FocusOut>", lambda event: self._commit(spec), add=True)
            self._boxes[spec.key] = box
            self._readers[spec.key] = lambda: box.get("1.0", "end")
            return box
        return None

    def _build_cli_row(self, parent: Any, row: int) -> None:
        """Buttons that probe arduino-cli (detect / test) live in the CLI tab."""
        palette = self.palette
        frame = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 14))
        frame.grid_columnconfigure(0, weight=1)
        self.cli_status = ctk.CTkLabel(frame, text="arduino-cli: not checked yet", anchor="w", justify="left",
                                       font=(palette.font_family, 10), text_color=palette.text_dim, wraplength=560)
        self.cli_status.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        ctk.CTkButton(frame, text="Detect", width=92, height=28, corner_radius=6, fg_color=palette.surface,
                      hover_color=palette.hover, text_color=palette.text, command=self._detect_cli)\
            .grid(row=0, column=1, sticky="e", padx=(6, 6), pady=10)
        self.test_button = ctk.CTkButton(frame, text="Test", width=84, height=28, corner_radius=6,
                                         fg_color=palette.accent, hover_color=palette.hover,
                                         text_color=palette.accent_text, command=self._test_cli)
        self.test_button.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=10)

    def _build_url_row(self, parent: Any, row: int) -> None:
        """Helper button that appends a well-known board manager URL."""
        palette = self.palette
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=1, sticky="e", padx=(0, 14), pady=(2, 10))
        ctk.CTkButton(frame, text="Add known URL\u2026", width=140, height=26, corner_radius=6,
                      fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                      command=self.add_known_url).grid(row=0, column=0, sticky="e")

    # -------------------------------------------------------------- read/write
    def reload(self) -> None:
        """Re-read every value from the settings object into the widgets."""
        settings = self._settings
        self._suppress = True
        try:
            for key in list(self._widgets):
                spec = self._spec_for(key)
                value = settings.get(key)
                try:
                    if spec is not None and spec.kind == "check":
                        self._checks[key].set(bool(value))
                    elif spec is not None and spec.kind == "combo":
                        self._combos[key].set(self._label_for(spec, value))
                    elif spec is not None and spec.kind == "list":
                        box = self._boxes[key]
                        box.delete("1.0", "end")
                        box.insert("1.0", "\n".join(str(item) for item in (value or [])))
                    else:
                        entry = self._entries.get(key)
                        if entry is not None:
                            entry.delete(0, "end")
                            entry.insert(0, "" if value is None else str(value))
                except (tk.TclError, AttributeError, TypeError) as exc:  # pragma: no cover
                    self._log.debug("could not fill %s: %s", key, exc)
        finally:
            self._suppress = False

    def _commit(self, spec: FieldSpec, chosen: Any = None, *, enter: bool = False) -> None:
        """Push one widget's value into the settings object (and notify)."""
        if self._suppress:
            return
        reader = self._readers.get(spec.key)
        if reader is None:
            return
        try:
            raw: Any = chosen if chosen is not None else reader()
        except (tk.TclError, AttributeError):  # pragma: no cover
            return
        old = self._settings.get(spec.key)
        value: Any = raw
        if spec.pairs:
            value = self._value_for_label(spec, str(raw))
        elif spec.kind == "int":
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                self._status(f"{spec.label}: '{raw}' is not a number - kept the previous value", ok=False)
                self.reload()
                return
            value = self._clamp(spec.key, value)
        elif spec.kind == "list":
            value = coerce_string_list(raw)
        else:
            value = str(raw).strip()
            if isinstance(old, int) and not isinstance(old, bool) and str(value).isdigit():
                value = int(value)     # e.g. the baud-rate combo stores an int
        if old == value:
            self._status(f"{spec.label} = {self._display(value)}")
            return
        self._settings.update(**{spec.key: value})
        self._status(f"{spec.label} = {self._display(value)}")
        if enter:
            self._log.debug("committed on Enter: %s=%r", spec.key, value)
        if self._on_change is not None:
            try:
                self._on_change(spec.key, value)
            except Exception:  # pragma: no cover - a callback must not break the form
                self._log.exception("settings change callback failed for %s", spec.key)

    def save_now(self) -> bool:
        """Write ``settings.json`` immediately; returns success."""
        if self._store is None:  # pragma: no cover
            self._status("no settings file is attached", ok=False)
            return False
        ok = bool(self._store.save())
        self._status("settings saved" if ok else "could not write settings.json", ok=ok)
        return ok

    def reset_current_tab(self) -> None:
        """Restore the defaults for the visible tab (with a confirmation)."""
        name = self.tabs.get()
        specs = SETTINGS_TABS.get(name, ())
        if not specs:
            return
        keys = [spec.key for spec in specs]
        if not ask_yes_no(self, f"Restore the {name} defaults?",
                          detail="These settings return to their built-in values:\n" + ", ".join(keys),
                          yes_label="Restore", kind="warning", palette=self.palette):
            return
        defaults = Settings()
        changes = {key: defaults.get(key) for key in keys}
        self._settings.update(**changes)
        self.reload()
        self._status(f"{name} settings restored to defaults")
        if self._on_change is not None:
            for key, value in changes.items():
                try:
                    self._on_change(key, value)
                except Exception:  # pragma: no cover
                    self._log.exception("settings change callback failed for %s", key)

    def add_known_url(self) -> None:
        """Append a URL from :data:`BOARD_MANAGER_URLS` to the list setting."""
        missing = self.suggested_urls()
        if not missing:
            self._status("all known board manager URLs are already listed")
            return
        chosen = ask_choice(self, list(missing), title="Add board manager URL",
                            label="Which platform's index should arduino-cli know about?",
                            display=lambda url: f"{_url_owner(str(url))}  -  {url}", palette=self.palette)
        if not chosen:
            return
        urls = list(self._settings.additional_board_urls or [])
        if chosen not in urls:
            urls.append(chosen)
        self._settings.update(additional_board_urls=urls)
        box = self._boxes.get("additional_board_urls")
        if box is not None:
            try:
                box.delete("1.0", "end")
                box.insert("1.0", "\n".join(str(item) for item in urls))
            except (tk.TclError, AttributeError):  # pragma: no cover
                pass
        self._status(f"added {chosen}")
        if self._on_change is not None:
            self._on_change("additional_board_urls", urls)

    def suggested_urls(self) -> tuple[str, ...]:
        """Well-known board manager URLs the user has not added yet."""
        have = {str(url).strip().lower() for url in (self._settings.additional_board_urls or [])}
        return tuple(url for url in BOARD_MANAGER_URLS.values() if str(url).lower() not in have)

    def focus_tab(self, name: str) -> None:
        """Switch to *name* (used by the toolbar and by error links)."""
        if name in SETTINGS_TABS:
            self.tabs.set(name)

    # ----------------------------------------------------------------- helpers
    def _browse(self, spec: FieldSpec) -> None:
        from tkinter import filedialog

        entry = self._entries.get(spec.key)
        initial = ""
        if entry is not None:
            try:
                initial = str(entry.get()).strip()
            except tk.TclError:  # pragma: no cover
                initial = ""
        try:
            if spec.browse == "dir":
                chosen = filedialog.askdirectory(parent=self, title=spec.label,
                                                 initialdir=initial or str(Path.home()))
            else:
                chosen = filedialog.askopenfilename(
                    parent=self, title=spec.label,
                    initialdir=str(Path(initial).expanduser().parent) if initial else str(Path.home()),
                    filetypes=[("arduino-cli", "arduino-cli*"), ("Executables", "*.exe *.bat *.cmd"),
                               ("All files", "*.*")],
                )
        except tk.TclError:  # pragma: no cover
            chosen = ""
        if not chosen or entry is None:
            return
        try:
            entry.delete(0, "end")
            entry.insert(0, str(Path(chosen)))
        except tk.TclError:  # pragma: no cover
            return
        self._commit(spec)

    def _clear(self, spec: FieldSpec) -> None:
        entry = self._entries.get(spec.key)
        if entry is not None:
            try:
                entry.delete(0, "end")
            except tk.TclError:  # pragma: no cover
                pass
        self._commit(spec)

    def _detect_cli(self) -> None:
        if self._on_detect_cli is not None:
            self._on_detect_cli()

    def _test_cli(self) -> None:
        if self._on_test_cli is not None:
            self._on_test_cli()

    def set_cli_status(self, text: str, ok: Optional[bool] = None) -> None:
        """Show the result of ``arduino-cli`` detection/testing (called by the app)."""
        color = self.palette.text_dim if ok is None else (self.palette.success if ok else self.palette.error)
        try:
            self.cli_status.configure(text=text, text_color=color)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _spec_for(self, key: str) -> Optional[FieldSpec]:
        for specs in SETTINGS_TABS.values():
            for spec in specs:
                if spec.key == key:
                    return spec
        return None

    def _label_for(self, spec: FieldSpec, value: Any) -> str:
        if spec.pairs:
            for stored, label in spec.pairs:
                if str(stored) == str(value):
                    return label
            return spec.pairs[0][1]
        text = "" if value is None else str(value)
        if not spec.options or not text:
            return text
        if text in spec.options:
            return text
        if spec.key.endswith("font_family"):
            return text          # unknown font: keep showing it, the user may have it installed
        return spec.options[0]

    def _value_for_label(self, spec: FieldSpec, label: str) -> str:
        for stored, shown in spec.pairs:
            if shown == label:
                return stored
        return label

    def _clamp(self, key: str, value: int) -> int:
        low, high = _LIMITS.get(key, (0, 1_000_000))
        return max(low, min(high, int(value)))

    def _display(self, value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, list):
            return f"{len(value)} item(s)" if value else "none"
        text = str(value) if value is not None else ""
        return text if len(text) <= 60 else text[:57] + "\u2026"

    def _status(self, message: str, ok: bool = True) -> None:
        try:
            self.status_label.configure(text=message,
                                       text_color=self.palette.text_dim if ok else self.palette.error)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _close(self) -> None:
        if self._on_close is not None:
            self._on_close()
            return
        try:
            self.grid_forget()
        except tk.TclError:  # pragma: no cover
            pass

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin the frame after a theme change (children keep their colours)."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
            self.title_label.configure(text_color=palette.text)
            self.hint_label.configure(text_color=palette.text_muted)
            self.save_button.configure(fg_color=palette.accent, hover_color=palette.accent_hover,
                                      text_color=palette.accent_text)
            self.close_button.configure(fg_color=palette.surface, hover_color=palette.hover)
            self.reset_button.configure(border_color=palette.border)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    @property
    def tab_names(self) -> tuple[str, ...]:
        """The tab names this view offers (handy for menus and tests)."""
        return tuple(SETTINGS_TABS)

    @property
    def setting_keys(self) -> tuple[str, ...]:
        """Every settings key this form can edit."""
        return tuple(spec.key for specs in SETTINGS_TABS.values() for spec in specs)


def _url_owner(url: str) -> str:
    """Human name for a board manager URL (``esp32:esp32`` style)."""
    for name, known in BOARD_MANAGER_URLS.items():
        if str(known) == url:
            return name
    return Path(url).name.split(".")[0] or "custom"
