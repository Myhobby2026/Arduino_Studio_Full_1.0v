"""Persistent application settings.

Everything the user can tweak lives in a single JSON document
(``%APPDATA%\\ArduinoStudio\\settings.json`` on Windows).  The class is a plain
python object with change listeners, so both the GUI and headless tools can
share one source of truth.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .utils import atomic_write_text, ensure_dir, get_logger

__all__ = ["SETTINGS_VERSION", "Settings", "SettingsStore"]

SETTINGS_VERSION = 1

DEFAULT_RECENT_LIMIT = 10

# Baud rates offered by the serial monitor drop down.
COMMON_BAUD_RATES: tuple[int, ...] = (
    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 250000, 460800, 921600,
)

LINE_ENDINGS: tuple[str, ...] = ("None", "LF", "CR", "CRLF")

_TERMINAL_SHELLS: tuple[str, ...] = ("powershell", "cmd")


@dataclass
class Settings:
    """Serializable user preferences with sensible defaults."""

    # ---- bookkeeping --------------------------------------------------------
    version: int = SETTINGS_VERSION
    first_run_completed: bool = False

    # ---- appearance ---------------------------------------------------------
    appearance_mode: str = "Dark"          # Dark | Light | System
    accent_color: str = "#2f81f7"
    ui_font_family: str = "Segoe UI"
    ui_font_size: int = 10
    window_width: int = 1320
    window_height: int = 820
    window_x: int = -1
    window_y: int = -1
    window_maximized: bool = False

    # ---- editor -------------------------------------------------------------
    editor_font_family: str = "Cascadia Code"
    editor_font_size: int = 12
    tab_size: int = 4
    insert_spaces: bool = True
    auto_indent: bool = True
    auto_close_brackets: bool = True
    auto_close_quotes: bool = True
    auto_complete: bool = True
    highlight_current_line: bool = True
    show_line_numbers: bool = True
    show_minimap_gutter: bool = True
    word_wrap: bool = False
    max_recent_projects: int = DEFAULT_RECENT_LIMIT
    autosave_interval_sec: int = 0          # 0 disables autosave
    trim_trailing_ws_on_save: bool = False
    ensure_final_newline_on_save: bool = True

    # ---- Arduino CLI --------------------------------------------------------
    arduino_cli_path: str = ""              # absolute path to arduino-cli(.exe)
    auto_detect_cli: bool = True
    arduino_data_dir: str = ""              # directories.data
    sketchbook_dir: str = ""                # directories.user (sketchbook)
    cli_config_file: str = ""                # optional --config-file
    cli_extra_args: str = ""                # appended to every cli invocation
    verbose_cli_output: bool = False
    compile_warnings: str = "default"        # none | default | all | enable-inhibit
    clean_build: bool = False
    build_output_mode: str = "project"       # project | temp
    check_missing_includes: bool = True      # offer library install on error

    # ---- boards / ports -----------------------------------------------------
    fqbn: str = "arduino:avr:uno"
    custom_fqbn: str = ""
    use_custom_fqbn: bool = False
    port: str = ""
    board_history: list[str] = field(default_factory=list)
    additional_board_urls: list[str] = field(default_factory=lambda: [
        "https://espressif.github.io/arduino-esp32/package_esp32_index.json",
        "https://arduino.esp8266.com/stable/package_esp8266com_index.json",
        "http://drazzy.com/package_drazzy.com_index.json",
    ])
    install_missing_cores: bool = True

    # ---- libraries ----------------------------------------------------------
    lib_auto_update_index: bool = False
    lib_index_max_age_days: int = 7
    lib_use_project_dir: bool = True        # prefer <project>/libraries for local libs

    # ---- serial monitor -----------------------------------------------------
    serial_baud: int = 9600
    serial_line_ending: str = "LF"
    serial_timestamps: bool = True
    serial_autoscroll: bool = True
    serial_show_rx: bool = True
    serial_show_tx: bool = True
    serial_hex_display: bool = False
    serial_toggle_dtr: bool = True
    serial_toggle_rts: bool = True
    serial_font_size: int = 10
    serial_auto_reopen_after_upload: bool = True
    serial_log_dir: str = ""

    # ---- console ------------------------------------------------------------
    console_font_size: int = 10
    console_max_lines: int = 5000
    console_timestamps: bool = False

    # ---- integrated terminal ------------------------------------------------
    terminal_shell: str = "powershell"       # powershell | cmd
    terminal_follow_project_dir: bool = True
    terminal_confirm_destructive: bool = True
    terminal_font_size: int = 10

    # ---- bootloader / programmer -------------------------------------------
    bootloader_programmer: str = "arduino"
    bootloader_confirm_required: bool = True
    bootloader_last_chip: str = "ATmega328P"
    bootloader_last_clock: str = "16 MHz external"
    bootloader_backup_dir: str = ""

    # ---- misc / lists -------------------------------------------------------
    recent_projects: list[str] = field(default_factory=list)
    open_files: list[str] = field(default_factory=list)
    active_project: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ utils
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON compatible snapshot of all settings."""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "Settings":
        """Build a :class:`Settings` from *data*, ignoring unknown keys."""
        data = dict(data or {})
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = {k: v for k, v in data.items() if k not in known}
        clean = {k: v for k, v in data.items() if k in known}
        settings = cls(**clean)  # type: ignore[arg-type]
        if unknown:
            settings.extra.update(unknown)
        settings._coerce()
        return settings

    def _coerce(self) -> None:
        """Normalise values loaded from disk (types may drift over versions)."""
        int_fields = (
            "version", "ui_font_size", "window_width", "window_height", "window_x", "window_y",
            "editor_font_size", "tab_size", "max_recent_projects", "autosave_interval_sec",
            "serial_baud", "serial_font_size", "console_font_size", "console_max_lines",
            "terminal_font_size", "lib_index_max_age_days",
        )
        for name in int_fields:
            value = getattr(self, name)
            try:
                setattr(self, name, int(value))
            except (TypeError, ValueError):
                setattr(self, name, Settings.__dataclass_fields__[name].default)  # type: ignore[attr-defined]
        bool_fields = (
            "first_run_completed", "insert_spaces", "auto_indent", "auto_close_brackets",
            "auto_close_quotes", "auto_complete", "highlight_current_line", "show_line_numbers",
            "show_minimap_gutter", "word_wrap", "window_maximized", "auto_detect_cli",
            "verbose_cli_output", "clean_build", "check_missing_includes", "use_custom_fqbn",
            "install_missing_cores", "lib_auto_update_index", "lib_use_project_dir",
            "serial_timestamps", "serial_autoscroll", "serial_show_rx", "serial_show_tx",
            "serial_hex_display", "serial_toggle_dtr", "serial_toggle_rts",
            "serial_auto_reopen_after_upload", "console_timestamps", "terminal_follow_project_dir",
            "terminal_confirm_destructive", "bootloader_confirm_required",
            "trim_trailing_ws_on_save", "ensure_final_newline_on_save",
        )
        for name in bool_fields:
            value = getattr(self, name)
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on"}
            setattr(self, name, bool(value))
        for name in ("board_history", "additional_board_urls", "recent_projects", "open_files"):
            value = getattr(self, name)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                value = []
            setattr(self, name, [str(v) for v in value])
        if not isinstance(self.extra, dict):
            self.extra = {}
        if self.appearance_mode not in {"Dark", "Light", "System"}:
            self.appearance_mode = "Dark"
        if self.terminal_shell not in _TERMINAL_SHELLS:
            self.terminal_shell = "powershell"
        if self.serial_line_ending not in LINE_ENDINGS:
            self.serial_line_ending = "LF"
        if self.compile_warnings not in {"none", "default", "all", "inhibit"}:
            self.compile_warnings = "default"
        if self.build_output_mode not in {"project", "temp"}:
            self.build_output_mode = "project"
        if self.tab_size < 1:
            self.tab_size = 4
        if self.editor_font_size < 6:
            self.editor_font_size = 10
        if self.serial_baud < 1:
            self.serial_baud = 9600
        # de-duplicate lists while preserving order
        for name in ("board_history", "additional_board_urls", "recent_projects", "open_files"):
            items = getattr(self, name)
            seen: list[str] = []
            for item in items:
                if item and item not in seen:
                    seen.append(item)
            setattr(self, name, seen)

    # --------------------------------------------------------- store bridge
    # ``_store`` is a plain class attribute (not a dataclass field) so it is
    # never serialised; widgets may therefore hold the :class:`Settings` object
    # itself and still persist changes through the owning store.
    _store = None

    def attach_store(self, store: "SettingsStore") -> None:
        """Link this object to the :class:`SettingsStore` that owns it."""
        object.__setattr__(self, "_store", store)

    @property
    def store(self) -> Optional["SettingsStore"]:
        """The store used for persistence (``None`` for a detached object)."""
        return self._store

    def get(self, key: str, default: Any = None) -> Any:
        """Read *key* from the settings (``extra`` first, then *default*)."""
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Write *key* and mark the settings dirty (through the store)."""
        self.update(**{key: value})

    def update(self, **changes: Any) -> None:
        """Apply several changes at once; a bound store also notifies listeners."""
        if not changes:
            return
        store = self._store
        if store is not None:
            store.update(**changes)
            return
        for key, value in changes.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.extra[key] = value

    def mark_dirty(self) -> None:
        """Ask the owning store to persist this object on the next save."""
        store = self._store
        if store is not None:
            store.mark_dirty()

    def reset_to_defaults(self) -> list[str]:
        """Restore every field to its dataclass default; returns the keys reset."""
        reference = Settings()
        names = [name for name in Settings.__dataclass_fields__ if name != "extra"]  # type: ignore[attr-defined]
        changed: dict[str, Any] = {}
        for name in names:
            current = getattr(self, name, None)
            default = getattr(reference, name)
            if isinstance(default, list):
                default = list(default)
            elif isinstance(default, dict):
                default = dict(default)
            if current != default:
                changed[name] = default
        self.update(**changed)
        return sorted(changed)

    # ------------------------------------------------------------- project list
    def remember_project(self, path: os.PathLike[str] | str) -> list[str]:
        """Move *path* to the front of the recent-project list."""
        resolved = str(Path(path).expanduser().resolve()) if Path(str(path)).is_absolute() else str(path)
        items = [resolved] + [p for p in self.recent_projects if str(Path(p).expanduser()) != resolved]
        self.recent_projects = items[: max(1, self.max_recent_projects)]
        return list(self.recent_projects)

    def forget_project(self, path: os.PathLike[str] | str) -> list[str]:
        """Drop *path* from the recent-project list (used when it disappears)."""
        resolved = str(Path(str(path)).expanduser())
        self.recent_projects = [p for p in self.recent_projects if str(Path(p).expanduser()) != resolved]
        return list(self.recent_projects)

    def remember_board(self, fqbn: str) -> list[str]:
        """Track recently used FQBNs (most recent first, max 8)."""
        if not fqbn:
            return list(self.board_history)
        self.board_history = ([fqbn] + [b for b in self.board_history if b != fqbn])[:8]
        return list(self.board_history)

    def effective_fqbn(self) -> str:
        """The FQBN to use, honouring the *custom FQBN* switch."""
        if self.use_custom_fqbn and self.custom_fqbn.strip():
            return self.custom_fqbn.strip()
        return self.fqbn or "arduino:avr:uno"

    def effective_cli_path(self) -> str:
        """Path of the arduino-cli executable configured by the user (may be empty)."""
        return self.arduino_cli_path.strip() if self.arduino_cli_path else ""


class SettingsStore:
    """Loads/saves :class:`Settings` and notifies listeners about changes.

    The store keeps exactly one :class:`Settings` instance alive for the whole
    application, so every screen reads and writes the same object.
    """

    def __init__(self, config_dir: os.PathLike[str] | str | None = None) -> None:
        self._log = get_logger("settings")
        self._dir = Path(config_dir) if config_dir else None
        self._listeners: list[Callable[[str, Any], Any]] = []
        self._settings: Settings = Settings()
        self._settings.attach_store(self)
        self._dirty = False
        self._loading = False
        self.path: Path = self._resolve_path()

    # ------------------------------------------------------------------ paths
    def _resolve_path(self) -> Path:
        base = self._dir or ensure_dir(_default_config_dir())
        return Path(base) / "settings.json"

    @property
    def directory(self) -> Path:
        """Directory that holds ``settings.json`` (also used for logs)."""
        return self.path.parent

    @property
    def log_dir(self) -> Path:
        return self.directory / "logs"

    @property
    def data_dir(self) -> Path:
        return self.directory / "data"

    # --------------------------------------------------------------- settings
    @property
    def settings(self) -> Settings:
        """The live settings object (mutating it directly is allowed)."""
        return self._settings

    def load(self) -> Settings:
        """Read ``settings.json`` (missing/broken files fall back to defaults)."""
        self._loading = True
        try:
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    self._log.warning("could not read %s (%s) - using defaults", self.path, exc)
                    try:
                        self.path.rename(self.path.with_suffix(".json.broken"))
                        self._log.info("moved corrupt settings file to %s", self.path.name + ".broken")
                    except OSError:
                        pass
                    raw = {}
                if isinstance(raw, dict):
                    self._settings = Settings.from_dict(raw)
                    self._settings.attach_store(self)
                    self._log.debug("loaded settings from %s", self.path)
            else:
                self._settings = Settings()
                self._settings.attach_store(self)
        finally:
            self._loading = False
        return self._settings

    def save(self, settings: Optional[Settings] = None) -> bool:
        """Persist settings; returns ``True`` on success."""
        if settings is not None:
            self._settings = settings
            settings.attach_store(self)
        try:
            ensure_dir(self.path.parent)
            payload = json.dumps(self._settings.to_dict(), indent=2, sort_keys=False)
            atomic_write_text(self.path, payload + "\n")
            self._dirty = False
            return True
        except OSError as exc:
            self._log.error("cannot save settings to %s: %s", self.path, exc)
            return False

    def update(self, **changes: Any) -> None:
        """Set several attributes at once and notify listeners once per key."""
        changed: list[tuple[str, Any]] = []
        for key, value in changes.items():
            if not hasattr(self._settings, key):
                self._settings.extra[key] = value
                changed.append((key, value))
                continue
            if getattr(self._settings, key) == value:
                continue
            setattr(self._settings, key, value)
            changed.append((key, value))
        if changed:
            self._dirty = True
            for key, value in changed:
                self._notify(key, value)

    def set(self, key: str, value: Any) -> None:
        """Set a single attribute (creating ``extra`` entries for unknown keys)."""
        self.update(**{key: value})

    def get(self, key: str, default: Any = None) -> Any:
        """Read a setting by name, falling back to *default*."""
        if hasattr(self._settings, key):
            return getattr(self._settings, key)
        return self._settings.extra.get(key, default)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty = True

    # -------------------------------------------------------------- listeners
    def add_listener(self, callback: Callable[[str, Any], Any]) -> None:
        """Register ``callback(key, new_value)`` for settings changes."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, Any], Any]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, key: str, value: Any) -> None:
        if self._loading:
            return
        for callback in list(self._listeners):
            try:
                callback(key, value)
            except Exception:  # pragma: no cover - a listener must not break others
                self._log.exception("settings listener failed for %r", key)

    # ---------------------------------------------------------------- housekeeping
    def forget_missing_projects(self) -> list[str]:
        """Remove recent entries whose folder no longer exists. Returns removed."""
        removed = [p for p in self._settings.recent_projects if not Path(p).is_dir()]
        for path in removed:
            self._settings.forget_project(path)
        if removed:
            self._dirty = True
        return removed


def _default_config_dir() -> Path:
    from .utils import app_config_dir

    return app_config_dir()


def coerce_string_list(value: Any) -> list[str]:
    """Utility used by dialogs to normalise multi-line list inputs."""
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[str] = value.splitlines()
    else:
        items = value  # type: ignore[assignment]
    return [item.strip() for item in items if str(item).strip()]
