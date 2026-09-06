"""``arduino-cli`` service: locating, probing and safely invoking the CLI.

This is the only module that knows how an ``arduino-cli`` command line looks.
Higher level features (compile, upload, libraries, bootloaders) call
:meth:`ArduinoCLI.execute` / :meth:`ArduinoCLI.execute_json` and add their own
parsing on top.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import process as proc
from .process import CommandError, CommandResult
from .utils import ensure_dir, get_logger, human_duration, is_windows

__all__ = [
    "ArduinoCLI",
    "CLIInfo",
    "CLIError",
    "BoardInfo",
    "PortInfo",
    "CoreInfo",
    "Diagnostics",
    "Diagnostic",
    "MemoryUsage",
    "CompileReport",
    "MIN_CLI_VERSION",
    "suggest_cli_download_url",
]

MIN_CLI_VERSION = (0, 32, 0)
DOWNLOAD_URL = "https://arduino.github.io/arduino-cli/latest/installer/"

_DIAG_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[\\/][^\t\n:]*|[\w./\\-]+\.(?:ino|cpp|c|h|hpp|S)):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>fatal error|error|warning|note|In function|undefined reference):\s*(?P<msg>.*)$"
)
_DIAG_RE_RELATIVE = re.compile(
    r"^(?P<file>[\w./\\+\-]+\.(?:ino|cpp|c|h|hpp)):(?P<line>\d+):(?P<col>\d+):\s*(?P<severity>fatal error|error|warning|note):\s*(?P<msg>.*)$"
)
_MISSING_INCLUDE_RE = re.compile(r"(?:fatal\s+)?(?:error|:\s*)\s*['\"]?(?P<header>[\w./+\-]+\.h(?:pp)?)['\"]?:?\s*No such file or directory", re.I)
_MISSING_INCLUDE_RE2 = re.compile(r"(?:fatal error:)?\s*unable to locate .*include file|no such file or directory.*(?P<header>[\w+\-.]+\.(?:h|hpp))", re.I)
_LIB_MISSING_RE = re.compile(r"library (?P<name>[A-Za-z0-9_.\-]+) not found", re.I)
_USAGE_SKETCH_RE = re.compile(r"Sketch uses\s+(?P<used>[\d,]+)\s+bytes\s*\((?P<pct>\d+)%\)\s*of program storage space\.?\s*Maximum is\s+(?P<max>[\d,]+)\s+bytes", re.I)
_USAGE_RAM_RE = re.compile(r"Global variables use\s+(?P<used>[\d,]+)\s+bytes\s*\((?P<pct>\d+)%\)\s*of dynamic memory,?\.?\s*leaving\s+(?P<free>[\d,]+)\s+bytes\s+for (?:local )?variables\.?\s*Maximum is\s+(?P<max>[\d,]+)\s+bytes", re.I)
_STEP_RE = re.compile(r"(?P<done>\d+)\s+of\s+(?P<total>\d+)")
_COMPILE_STAGES = (
    "resolving libraries", "detecting libraries", "compiling sketch", "compiling core",
    "linking", "calculating", "building", "uploading", "hard resetting", "verifying", "avrdude: writing",
)


class CLIError(RuntimeError):
    """Arduino CLI is missing, unusable or returned a hard failure."""


def suggest_cli_download_url() -> str:
    """Web page with the official installers (used by the setup wizard)."""
    return DOWNLOAD_URL


@dataclass
class CLIInfo:
    """What ``arduino-cli version`` + ``config dump`` told us."""

    path: str = ""
    version: str = ""
    date: str = ""
    ok: bool = False
    message: str = ""
    data_dir: str = ""
    user_dir: str = ""
    additional_urls: list[str] = field(default_factory=list)
    config_file: str = ""

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)

    @property
    def supported(self) -> bool:
        if not self.ok or not self.version:
            return False
        return self.version_tuple >= MIN_CLI_VERSION

    def describe(self) -> str:
        if not self.ok:
            return self.message or "arduino-cli not found"
        return f"arduino-cli {self.version or '?'} at {self.path}"


@dataclass
class BoardInfo:
    """One entry of ``arduino-cli board listall``."""

    name: str
    fqbn: str
    vendor: str = ""
    core: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name}  [{self.fqbn}]"


@dataclass
class PortInfo:
    """A serial/COM port, optionally with the board attached to it."""

    address: str
    description: str = ""
    device: str = ""
    protocol: str = "serial"
    board_fqbn: str = ""
    board_name: str = ""
    vid: str = ""
    pid: str = ""
    serial_number: str = ""
    manufacturer: str = ""
    hwid: str = ""

    @property
    def is_serial(self) -> bool:
        return (self.protocol or "serial").lower() in {"serial", "canvas", ""}

    @property
    def label(self) -> str:
        parts = [self.address]
        if self.board_name:
            parts.append(self.board_name)
        elif self.description:
            parts.append(self.description)
        if self.vid and self.pid:
            parts.append(f"VID:PID={self.vid}:{self.pid}")
        return " - ".join(parts)


@dataclass
class CoreInfo:
    """A platform/core returned by ``arduino-cli core list``."""

    name: str                       # arduino:avr
    version: str = ""
    status: str = ""                # "installed" | "update available" ...
    latest: str = ""
    boards: list[str] = field(default_factory=list)
    install_dir: str = ""

    @property
    def update_available(self) -> bool:
        return bool(self.latest) and self.latest != self.version


@dataclass
class Diagnostic:
    """One compiler message with file/line so the editor can jump to it."""

    file: str
    line: int
    column: int
    severity: str                   # error | warning | note | fatal error
    message: str
    project_relative: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity in {"error", "fatal error"}


@dataclass
class Diagnostics:
    """Parsed compile output: messages + memory usage + missing libraries."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    missing_includes: list[str] = field(default_factory=list)
    missing_libraries: list[str] = field(default_factory=list)
    memory: Optional[MemoryUsage] = None
    linker_errors: list[str] = field(default_factory=list)
    output: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.errors) or bool(self.linker_errors)


@dataclass
class MemoryUsage:
    flash_bytes: int = 0
    flash_max: int = 0
    flash_percent: float = 0.0
    ram_bytes: int = 0
    ram_max: int = 0
    ram_percent: float = 0.0
    ram_free: int = 0

    def summary(self) -> str:
        parts = []
        if self.flash_max:
            parts.append(f"Flash: {self.flash_bytes:,} / {self.flash_max:,} bytes ({self.flash_percent:.0f}%)")
        if self.ram_max:
            parts.append(f"RAM: {self.ram_bytes:,} / {self.ram_max:,} bytes ({self.ram_percent:.0f}%)")
        return "   |   ".join(parts)


@dataclass
class CompileReport:
    """Result of ``arduino-cli compile`` including the parsed diagnostics."""

    ok: bool
    returncode: int
    duration: float
    build_dir: str
    binary_path: str
    output: str
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    cancelled: bool = False
    fqbn: str = ""

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration)

    @property
    def error_count(self) -> int:
        return len(self.diagnostics.errors)

    @property
    def warning_count(self) -> int:
        return len(self.diagnostics.warnings)


def parse_version(text: str) -> tuple[int, int, int]:
    """``"1.0.4"`` -> ``(1, 0, 4)``; unparsable input yields ``(0, 0, 0)``."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return (0, 0, 0)
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


class ArduinoCLI:
    """Wrapper around the ``arduino-cli`` binary.

    The wrapper is stateless with respect to the GUI: it can be used from any
    worker thread and never touches Tkinter.
    """

    def __init__(
        self,
        cli_path: str = "",
        config_file: str = "",
        extra_args: str = "",
        cwd: os.PathLike[str] | str | None = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._log = get_logger("cli")
        self._cli_path = (cli_path or "").strip()
        self.config_file = (config_file or "").strip()
        self.extra_args = extra_args or ""
        self.cwd = str(cwd) if cwd else None
        self.env = dict(env or {})
        self._info: Optional[CLIInfo] = None
        self._version_lock = __import__("threading").Lock()

    # ------------------------------------------------------------- locating
    @property
    def cli_path(self) -> str:
        """Absolute path to the executable (empty when not resolved yet)."""
        return self._cli_path

    @cli_path.setter
    def cli_path(self, value: str) -> None:
        self._cli_path = (value or "").strip()
        self._info = None

    @staticmethod
    def auto_detect(extra_dirs: Iterable[os.PathLike[str] | str] = ()) -> str:
        """Search PATH and the usual Windows install locations."""
        return proc.find_executable("arduino-cli", extra_dirs=tuple(extra_dirs))

    def resolve(self, preferred: str = "", auto_detect: bool = True) -> str:
        """Return a usable executable path (or raise :class:`CLIError`)."""
        for candidate in (preferred, self._cli_path):
            if candidate and Path(candidate).is_file():
                self._cli_path = str(candidate)
                return self._cli_path
        if auto_detect:
            found = self.auto_detect()
            if found:
                self._cli_path = found
                return found
        raise CLIError(
            "arduino-cli was not found. Install it (see the setup wizard) and point "
            "Arduino Studio at the executable."
        )

    # ------------------------------------------------------------- probing
    def probe(self, path: str = "") -> CLIInfo:
        """Check the binary: ``arduino-cli version`` + ``config dump``.

        Never raises - the returned :class:`CLIInfo` carries the message so the
        UI can show it in the settings / wizard.
        """
        target = path or self._cli_path
        if not target:
            found = self.auto_detect()
            if not found:
                return CLIInfo(ok=False, message="arduino-cli not found. Browse for arduino-cli.exe to continue.")
            target = found
        exe = Path(target)
        if not exe.exists():
            return CLIInfo(path=str(exe), ok=False, message=f"No such file: {exe}")
        if exe.is_dir():
            for name in ("arduino-cli.exe", "arduino-cli"):
                candidate = exe / name
                if candidate.is_file():
                    exe = candidate
                    target = str(candidate)
                    break
            else:
                return CLIInfo(path=str(exe), ok=False, message="That folder does not contain arduino-cli.")
        try:
            result = proc.run_capture([str(exe), "version"], timeout=20.0)
        except CommandError as exc:
            return CLIInfo(path=str(exe), ok=False, message=str(exc))
        text = result.output or ""
        if result.returncode != 0:
            return CLIInfo(path=str(exe), ok=False, message=f"'{exe.name} version' failed: {text.strip()[:300]}")
        version = ""
        date = ""
        match = re.search(r"Version:\s*([0-9][\w.\-]*)", text)
        if match:
            version = match.group(1)
        match = re.search(r"Date:\s*(.+)", text)
        if match:
            date = match.group(1).strip()
        if not version:
            match = re.search(r"arduino-cli\s+(\d+\.\d+\.\d+)", text)
            version = match.group(1) if match else ""
        info = CLIInfo(path=str(exe), version=version, date=date, ok=True, message=text.strip().splitlines()[0] if text.strip() else "")
        self._cli_path = str(exe)
        self._info = info
        self._fill_config(info)
        return info

    def _fill_config(self, info: CLIInfo) -> None:
        """Best effort ``config dump`` merge (directories, additional URLs)."""
        try:
            data = self.execute_json(["config", "dump"], timeout=20.0, ok_required=False)
        except (CommandError, CLIError):
            return
        if not isinstance(data, dict):
            return
        directories = data.get("directories") or {}
        info.data_dir = str(directories.get("data") or "")
        info.user_dir = str(directories.get("user") or "")
        manager = data.get("board_manager") or {}
        urls = manager.get("additional_urls") or []
        info.additional_urls = [str(u) for u in urls if u]
        info.config_file = str(data.get("__config_file__") or "") or self.config_file

    @property
    def info(self) -> Optional[CLIInfo]:
        return self._info

    @property
    def version(self) -> str:
        return self._info.version if self._info else ""

    @property
    def ready(self) -> bool:
        return bool(self._info and self._info.ok)

    def require(self) -> str:
        """Executable path, raising :class:`CLIError` when unavailable."""
        if self._cli_path and Path(self._cli_path).is_file():
            return self._cli_path
        return self.resolve()

    @property
    def data_dir(self) -> Path:
        """``directories.data`` (where cores and libraries are installed)."""
        if self._info and self._info.data_dir:
            return Path(self._info.data_dir)
        return self.default_data_dir()

    @staticmethod
    def default_data_dir() -> Path:
        """Where arduino-cli stores packages/cores when no config overrides it."""
        if is_windows():
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            candidate = Path(local) / "arduino-cli"
            if candidate.is_dir():
                return candidate
            legacy = Path(os.environ.get("APPDATA") or "") / "arduino15"
            if legacy.is_dir():
                return legacy
            return candidate
        return Path.home() / ".arduino15"

    @staticmethod
    def default_sketchbook_dir() -> Path:
        if is_windows():
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            candidate = Path(local) / "Arduino"
            if candidate.is_dir():
                return candidate
            documents = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents" / "Arduino"
            return documents if documents.is_dir() else candidate
        return Path.home() / "Arduino"

    @property
    def sketchbook_dir(self) -> Path:
        if self._info and self._info.user_dir:
            return Path(self._info.user_dir)
        return self.default_sketchbook_dir()

    # ------------------------------------------------------------- plumbing
    def global_args(self) -> list[str]:
        """Flags applied to every invocation."""
        args: list[str] = []
        if self.config_file and Path(self.config_file).is_file():
            args += ["--config-file", self.config_file]
        if self.extra_args.strip():
            try:
                args += shlex.split(self.extra_args, posix=not is_windows())
            except ValueError:
                args += self.extra_args.split()
        return args

    def command(self, args: Sequence[str]) -> list[str]:
        """Full argv for ``args`` (binary + global flags + command)."""
        return [self.require(), *self.global_args(), *[str(a) for a in args]]

    def execute(
        self,
        args: Sequence[str],
        *,
        cwd: os.PathLike[str] | str | None = None,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        timeout: Optional[float] = None,
        progress: Optional[Callable[[Optional[float], str], Any]] = None,
        echo_command: bool = True,
        label: str = "",
    ) -> CommandResult:
        """Run ``arduino-cli <args>`` streaming its output to ``on_line``."""
        argv = self.command(args)
        if echo_command and on_line is not None:
            on_line("$ " + " ".join(shlex.quote(part) for part in argv))

        def _line(text: str) -> None:
            if on_line is not None:
                on_line(text)
            if progress is not None:
                fraction, message = _progress_from_line(text)
                if message or fraction is not None:
                    progress(fraction, message or text[:60])

        try:
            return proc.run_streaming(
                argv, cwd=cwd or self.cwd, env=self.env or None, timeout=timeout,
                context=context, on_line=_line if (on_line or progress) else None,
            )
        except CommandError as exc:
            message = (
                f"Cannot run arduino-cli: {exc}\n"
                "Open Settings -> Arduino CLI and set the path to arduino-cli.exe "
                "(or install arduino-cli from the setup wizard)."
            )
            raise CLIError(message) from exc

    def execute_json(
        self,
        args: Sequence[str],
        *,
        cwd: os.PathLike[str] | str | None = None,
        timeout: float = 45.0,
        ok_required: bool = True,
    ) -> Any:
        """Run a command with ``--format json`` and return the parsed payload."""
        argv_args = [*args]
        if "--format" not in argv_args and "-f" not in argv_args:
            argv_args += ["--format", "json"]
        result = proc.run_capture(self.command(argv_args), cwd=cwd or self.cwd, timeout=timeout)
        data = proc.parse_json_output(result.output, default=None)
        if data is None:
            if ok_required and result.returncode != 0:
                raise CLIError(self.humanize_failure(result.output) or f"arduino-cli exited with {result.returncode}")
            # fall back to unstructured data so callers can still show text
            return None
        if ok_required and result.returncode != 0 and isinstance(data, dict) and data.get("error"):
            raise CLIError(str(data["error"]))
        return data

    # ---------------------------------------------------------- humanising
    @staticmethod
    def humanize_failure(output: str) -> str:
        """Turn common CLI errors into actionable advice (short paragraph)."""
        text = (output or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        hints = {
            "first run": "Run 'arduino-cli core update-index' (Settings -> Arduino CLI) to download the platform index.",
            "not found": "The board platform is not installed. Install it from the Cores list.",
            "no such file or directory": "A required file is missing - check the sketch folder and build path.",
            "permission denied": "Windows blocked the operation. Close the Serial Monitor / Arduino IDE and retry.",
            "access is denied": "The COM port is busy. Close the Serial Monitor (and any other app using it) and retry.",
            "the port is busy": "The COM port is busy. Close the Serial Monitor and retry.",
            "avrdude: ser_open": "Cannot open the COM port: it is in use or the wrong port is selected.",
            "avrdude: error: programmer not responding": "The programmer is not responding - check wiring/driver, or lower the programmer speed.",
            "does not match the signature": "The chip does not match the selected board definition (wrong MCU/fuses?).",
            "espstool": "esptool failed - hold BOOT/FLASH while resetting the ESP board into download mode.",
            "connection reset": "A download was interrupted - check the network or retry.",
            "unable to verify": "Checksum verification failed; the upload may have been interrupted.",
        }
        extras = [hint for key, hint in hints.items() if key in lowered]
        first_error = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(("error", "fatal", "avrdude: error", "esptool", "exit status")):
                first_error = stripped
                break
        parts = []
        if first_error:
            parts.append(first_error[:300])
        parts.extend(extras[:3])
        return "\n".join(dict.fromkeys(parts)) or text.splitlines()[-1][:300]

    # -------------------------------------------------------- board / ports
    def board_listall(self, include_hidden: bool = False) -> list[BoardInfo]:
        """All boards known to the CLI (installed cores + indexes)."""
        args = ["board", "listall", "--format", "json"]
        if include_hidden:
            args.append("--include-hidden")
        data = self.execute_json(args, timeout=60.0)
        boards: list[BoardInfo] = []
        for entry in _extract_list(data, "boards"):
            fqbn = str(entry.get("fqbn") or entry.get("id") or "")
            if not fqbn:
                continue
            props = entry.get("properties") or {}
            boards.append(BoardInfo(
                name=str(entry.get("name") or fqbn),
                fqbn=fqbn,
                vendor=fqbn.split(":")[0],
                core=":".join(fqbn.split(":")[:2]),
                properties=props if isinstance(props, dict) else {},
            ))
        boards.sort(key=lambda b: b.name.lower())
        return boards

    def board_list(self, timeout: float = 12.0) -> list[PortInfo]:
        """Ports detected by ``arduino-cli board list``."""
        try:
            data = self.execute_json(["board", "list"], timeout=timeout)
        except (CLIError, CommandError) as exc:
            self._log.debug("board list failed: %s", exc)
            return []
        ports: list[PortInfo] = []
        for entry in _extract_list(data, "detected_ports", "ports"):
            if not isinstance(entry, dict):
                continue
            fields = _port_fields(entry)
            board_entry = _first_board(entry)
            ports.append(PortInfo(
                address=fields["address"],
                description=fields["label"],
                device=fields["address"],
                protocol=fields["protocol"],
                board_fqbn=str(board_entry.get("fqbn") or "") if isinstance(board_entry, dict) else "",
                board_name=str(board_entry.get("name") or "") if isinstance(board_entry, dict) else "",
                vid=fields["vid"],
                pid=fields["pid"],
                serial_number=fields["serial_number"],
                hwid=fields["hwid"],
            ))
        return [port for port in ports if port.address]

    def upload_port_list(self, fqbn: str = "", timeout: float = 15.0) -> list[PortInfo]:
        """``arduino-cli upload-port list`` (1.x) - only ports usable for upload."""
        args = ["upload-port", "list"]
        if fqbn:
            args += ["--fqbn", fqbn]
        try:
            data = self.execute_json(args, timeout=timeout)
        except (CLIError, CommandError) as exc:
            # `upload-port list` only exists in newer CLIs; fall back to `board list`
            self._log.debug("upload-port list unavailable (%s) - using board list", exc)
            return self.board_list(timeout=timeout)
        out: list[PortInfo] = []
        for entry in _extract_list(data, "detected_ports", "ports"):
            if isinstance(entry, dict):
                fields = _port_fields(entry)
                if fields["address"]:
                    out.append(PortInfo(address=fields["address"], description=fields["label"],
                                        device=fields["address"], protocol=fields["protocol"]))
            elif isinstance(entry, str) and entry:
                out.append(PortInfo(address=entry))
        if not out:
            self._log.debug("upload-port list returned nothing - falling back to board list")
            return self.board_list(timeout=timeout)
        return out

    def board_attach(self, sketch: os.PathLike[str] | str, port: str, fqbn: str) -> CommandResult:
        """Persist ``board.port`` / FQBN in the sketch (``board attach``)."""
        args = ["board", "attach"]
        if port:
            args += ["--port", port]
        if fqbn:
            args += ["--fqbn", fqbn]
        args.append(str(sketch))
        return self.execute(args, timeout=60.0, echo_command=True)

    # ---------------------------------------------------------------- cores
    def core_list(self) -> list[CoreInfo]:
        """Installed platforms (``arduino-cli core list``)."""
        data = self.execute_json(["core", "list"], timeout=45.0)
        entries = _extract_list(data, "installed_cores", "cores", "installed")
        cores = [_parse_core(entry) for entry in entries]
        return [core for core in cores if core.name]

    def core_search(self, term: str = "") -> list[CoreInfo]:
        args = ["core", "search"]
        if term:
            args.append(term)
        data = self.execute_json(args, timeout=90.0)
        out: list[CoreInfo] = []
        for entry in _extract_list(data, "searched_cores", "cores"):
            info = _parse_core(entry)
            latest = entry.get("latest") if isinstance(entry, dict) else None
            if isinstance(latest, dict):
                info.latest = str(latest.get("version") or "")
            out.append(info)
        return out

    def core_install(self, names: Sequence[str], on_line: Optional[Callable[[str], Any]] = None,
                     context: Any = None) -> CommandResult:
        if not names:
            raise CLIError("No core selected.")
        return self.execute(["core", "install", *names], on_line=on_line, context=context, timeout=None)

    def core_uninstall(self, name: str, context: Any = None) -> CommandResult:
        return self.execute(["core", "uninstall", name], context=context, timeout=None)

    def update_index(self, on_line: Optional[Callable[[str], Any]] = None, context: Any = None) -> CommandResult:
        return self.execute(["core", "update-index"], on_line=on_line, context=context, timeout=None)

    def lib_update_index(self, on_line: Optional[Callable[[str], Any]] = None, context: Any = None) -> CommandResult:
        return self.execute(["lib", "update-index"], on_line=on_line, context=context, timeout=None)

    def ensure_additional_urls(self, urls: Sequence[str]) -> list[str]:
        """Add missing board manager URLs to the CLI config. Returns added ones."""
        wanted = [u.strip() for u in urls if u and u.strip()]
        if not wanted:
            return []
        try:
            data = self.execute_json(["config", "dump"], timeout=25.0) or {}
        except (CLIError, CommandError):
            data = {}
        manager = data.get("board_manager") if isinstance(data, dict) else {}
        current = {str(u) for u in ((manager or {}).get("additional_urls") or [])}
        added: list[str] = []
        for url in wanted:
            if url in current:
                continue
            result = self.execute(["config", "add", "board_manager.additional_urls", url], timeout=25.0)
            if result.returncode == 0:
                added.append(url)
            else:
                result = self.execute(["config", "set", "board_manager.additional_urls", ",".join(sorted(current | {url}))], timeout=25.0)
                if result.returncode == 0:
                    added.append(url)
                else:
                    self._log.warning("could not register board manager URL %s: %s", url, result.output[:200])
        return added

    # ------------------------------------------------------- compile/upload
    def compile(
        self,
        sketch_dir: os.PathLike[str] | str,
        fqbn: str,
        *,
        build_dir: os.PathLike[str] | str | None = None,
        libraries: Sequence[os.PathLike[str] | str] = (),
        warnings: str = "default",
        clean: bool = False,
        verbose: bool = False,
        build_properties: Optional[dict[str, str]] = None,
        optimize_debug: bool = False,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        progress: Optional[Callable[[Optional[float], str], Any]] = None,
    ) -> CompileReport:
        """Compile a sketch folder and parse the result.

        ``sketch_dir`` must be a folder whose name matches its main ``.ino``.
        """
        args = ["compile", str(sketch_dir), "--fqbn", fqbn]
        if build_dir:
            args += ["--build-path", str(build_dir)]
        if clean:
            args.append("--clean")
        if verbose:
            args += ["-v"]
        if warnings and warnings != "default":
            args += ["--warnings", "all" if warnings == "all" else "none"]
        if optimize_debug:
            args.append("--optimize-for-debug")
        for library in libraries:
            args += ["--libraries", str(library)]
        for key, value in (build_properties or {}).items():
            args += ["--build-property", f"{key}={value}"]
        try:
            result = self.execute(args, on_line=on_line, context=context, timeout=None, progress=progress)
        except CLIError as exc:
            return CompileReport(False, 1, 0.0, str(build_dir or ""), "", str(exc), fqbn=fqbn)
        report = self.analyze_compile(result, sketch_dir=sketch_dir, build_dir=build_dir or Path(sketch_dir) / "build")
        report.fqbn = fqbn
        return report

    def analyze_compile(self, result: CommandResult, sketch_dir: os.PathLike[str] | str,
                        build_dir: os.PathLike[str] | str) -> CompileReport:
        """Parse a finished compile: diagnostics, memory usage, binary path."""
        diagnostics = self.parse_output(result.output, sketch_dir)
        binary = ""
        build_path = Path(build_dir)
        ino = Path(sketch_dir)
        if ino.is_dir():
            for candidate in (
                build_path / f"{ino.name}.elf",
                build_path / f"{ino.name}.hex",
            ):
                if candidate.is_file():
                    binary = str(candidate)
                    break
        if not binary:
            matches = sorted(build_path.glob("*.elf"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True) \
                if build_path.is_dir() else []
            if matches:
                binary = str(matches[0])
        ok = result.returncode == 0 and not diagnostics.errors and not result.cancelled
        return CompileReport(
            ok=ok,
            returncode=result.returncode,
            duration=result.duration,
            build_dir=str(build_path),
            binary_path=binary,
            output=result.output,
            diagnostics=diagnostics,
            cancelled=result.cancelled,
        )

    def upload(
        self,
        *,
        build_dir: os.PathLike[str] | str,
        fqbn: str,
        port: str,
        verify: bool = False,
        verbose: bool = False,
        no_reset: bool = False,
        programmer: str = "",
        baud: Optional[int] = None,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        progress: Optional[Callable[[Optional[float], str], Any]] = None,
    ) -> CommandResult:
        """Upload an already compiled build directory to *port*."""
        args = ["upload", "--input-dir", str(build_dir), "--fqbn", fqbn, "--port", port]
        if verify:
            args.append("--verify")
        if verbose:
            args += ["-v"]
        if no_reset:
            args.append("--no-reset")
        if programmer:
            args += ["--programmer", programmer]
        if baud:
            args += ["-b", str(int(baud))]
        return self.execute(args, on_line=on_line, context=context, timeout=None, progress=progress)

    def burn_bootloader(
        self,
        *,
        fqbn: str,
        port: str,
        programmer: str,
        sketch_dir: os.PathLike[str] | str | None = None,
        verbose: bool = True,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        progress: Optional[Callable[[Optional[float], str], Any]] = None,
    ) -> CommandResult:
        """``arduino-cli burn-bootloader`` (compiles the bootloader + programs flash/fuses)."""
        args = ["burn-bootloader", "--fqbn", fqbn, "--programmer", programmer]
        if port:
            args += ["--port", port]
        if verbose:
            args += ["-v"]
        if sketch_dir:
            args.append(str(sketch_dir))
        return self.execute(args, on_line=on_line, context=context, timeout=None, progress=progress)

    def cache_clean(self) -> CommandResult:
        return self.execute(["cache", "clean"], timeout=120.0)

    # ------------------------------------------------------------- analysis
    def parse_output(self, text: str, sketch_dir: os.PathLike[str] | str) -> Diagnostics:
        """Parse gcc/arduino-cli output into diagnostics + memory usage."""
        data = Diagnostics(output=text or "")
        root = Path(sketch_dir).resolve() if sketch_dir else None
        seen: set[tuple[str, int, int, str, str]] = set()
        for raw in (text or "").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            diag = self._parse_diag(line, root)
            if diag is not None:
                key = (diag.file, diag.line, diag.column, diag.severity, diag.message)
                if key not in seen:
                    seen.add(key)
                    data.diagnostics.append(diag)
                    (data.errors if diag.is_error else data.warnings).append(diag)
                # "fatal error: DHT.h: No such file..." is a diagnostic *and* a
                # missing include, so keep scanning the same line.
                self._collect_missing(line, data)
                continue
            match = _USAGE_SKETCH_RE.search(line)
            if match:
                data.memory = data.memory or MemoryUsage()
                assert data.memory is not None
                data.memory.flash_bytes = _as_int(match.group("used"))
                data.memory.flash_max = _as_int(match.group("max"))
                data.memory.flash_percent = _as_int(match.group("pct"), 0) / 1.0
            match = _USAGE_RAM_RE.search(line)
            if match:
                data.memory = data.memory or MemoryUsage()
                assert data.memory is not None
                data.memory.ram_bytes = _as_int(match.group("used"))
                data.memory.ram_max = _as_int(match.group("max"))
                data.memory.ram_percent = _as_int(match.group("pct"), 0) / 1.0
                data.memory.ram_free = _as_int(match.group("free"))
            if "undefined reference to" in line or "multiple definition of" in line:
                data.linker_errors.append(line.strip()[:300])
            if line.strip().startswith(("avrdude: error", "esptool", "A fatal error occurred")):
                data.errors.append(Diagnostic(file="", line=0, column=0, severity="error", message=line.strip()[:300]))
            self._collect_missing(line, data)
            lib_missing = _LIB_MISSING_RE.search(line)
            if lib_missing:
                name = lib_missing.group("name")
                if name.lower() not in {n.lower() for n in data.missing_libraries}:
                    data.missing_libraries.append(name)
        return data

    @staticmethod
    def _collect_missing(line: str, data: "Diagnostics") -> None:
        """Record missing include files / libraries mentioned by *line*."""
        missing = _MISSING_INCLUDE_RE.search(line)
        header = ""
        if missing:
            header = missing.group("header")
        else:
            missing2 = _MISSING_INCLUDE_RE2.search(line)
            if missing2 and missing2.groupdict().get("header"):
                header = missing2.group("header")
        if header and header not in data.missing_includes:
            data.missing_includes.append(header)

    @staticmethod
    def _parse_diag(line: str, root: Optional[Path]) -> Optional[Diagnostic]:
        for pattern in (_DIAG_RE, _DIAG_RE_RELATIVE):
            match = pattern.match(line.strip())
            if not match:
                continue
            file_part = match.group("file").replace("\\", "/")
            severity = match.group("severity")
            message = match.group("msg").strip()
            relative = ""
            if root is not None:
                try:
                    relative = (root / file_part).resolve().relative_to(root).as_posix()
                except (ValueError, OSError):
                    # build paths reference /tmp or the core dir - keep only the file name
                    if file_part.startswith(root.as_posix()):
                        relative = file_part[len(root.as_posix()) + 1:]
            if not relative:
                # Build paths may live outside the project (sketch cache, core
                # headers).  Keep the base name so the UI can still find the file.
                base = Path(file_part).name
                if base and base != file_part:
                    relative = base
                elif base:
                    relative = base
            return Diagnostic(
                file=file_part,
                line=_as_int(match.group("line"), 0),
                column=_as_int(match.group("col"), 0),
                severity=severity,
                message=message,
                project_relative=relative,
            )
        return None

    def suggest_libraries_for_headers(self, headers: Sequence[str]) -> dict[str, str]:
        """Map include file names to likely library names via ``lib search``.

        Returns ``{header: library name}`` (only headers with a hit).
        """
        suggestions: dict[str, str] = {}
        for header in headers:
            stem = re.sub(r"\.(h|hpp)$", "", header, flags=re.I)
            for term in (stem, header):
                if not term:
                    continue
                try:
                    data = self.execute_json(["lib", "search", term], timeout=40.0, ok_required=False)
                except (CLIError, CommandError):
                    continue
                for entry in _extract_list(data, "searched_libraries", "libraries"):
                    name = str((entry or {}).get("name") or "")
                    if not name:
                        continue
                    extras = [str(x) for x in ((entry.get("includes") or []) if isinstance(entry, dict) else [])]
                    if header in extras or name.lower() == term.lower() or term.lower() in name.lower():
                        suggestions[header] = name
                        break
                if header in suggestions:
                    break
        return suggestions

    # ------------------------------------------------------------- utilities
    def build_libraries_for(self, project_root: os.PathLike[str] | str) -> list[str]:
        """``--libraries`` args: project ``libraries/`` + ``src`` subfolders."""
        root = Path(project_root)
        out: list[str] = []
        for folder in ("libraries", "src"):
            candidate = root / folder
            if candidate.is_dir():
                out.append(str(candidate))
        return out

    def write_build_config_note(self, sketch_dir: os.PathLike[str] | str, fqbn: str, port: str) -> None:
        """Store ``port``/``fqbn`` in ``project.json`` adjacent data (best effort)."""
        target = Path(sketch_dir) / "arduino_studio_build.json"
        try:
            target.write_text(json.dumps({"fqbn": fqbn, "port": port}, indent=2), encoding="utf-8")
        except OSError:
            self._log.debug("cannot write build note next to sketch")

    @staticmethod
    def format_argv(argv: Sequence[str]) -> str:
        return " ".join(shlex.quote(str(part)) for part in argv)


# --------------------------------------------------------------------------------- helpers
def _progress_from_line(line: str) -> tuple[Optional[float], str]:
    """Derive ``(fraction, message)`` from an arduino-cli output line."""
    text = line.strip()
    if not text:
        return None, ""
    lowered = text.lower()
    match = _STEP_RE.search(lowered)
    if match and any(word in lowered for word in ("file", "compil", "steps")):
        done, total = _as_int(match.group("done"), 0), _as_int(match.group("total"), 0)
        if total > 0:
            return min(0.99, done / total), f"{done}/{total}"
    for index, stage in enumerate(_COMPILE_STAGES):
        if stage in lowered:
            return (index + 1) / (len(_COMPILE_STAGES) + 2), text[:70]
    if lowered.startswith("uploading") or "avrdude" in lowered:
        return None, text[:70]
    return None, ""


def _extract_list(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the first list found under *keys* (or the list itself)."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, (dict, list))]
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, (dict, list)) or isinstance(item, str)]
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return [item for item in value if isinstance(item, dict)]
    return []


def _port_fields(entry: dict[str, Any]) -> dict[str, str]:
    """Normalise one ``board list`` entry into flat strings.

    Older CLIs nest the port inside ``{"port": {"address": ..., "label": ...}}``,
    newer ones use ``port_address`` / ``port_label`` or a ``config`` object, and
    some builds return ``port`` as the address string alone - all of them land
    here, so a dict never leaks into the UI.
    """
    nested = entry.get("port") if isinstance(entry.get("port"), dict) else {}
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    identification = config.get("identification") if isinstance(config.get("identification"), dict) else {}

    def pick(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
        return ""

    address = pick(entry.get("port_address"), nested.get("address"), entry.get("address"),
                   entry.get("port"), config.get("port"))
    label = pick(entry.get("port_label"), nested.get("label"), entry.get("label"), config.get("description"))
    protocol = pick(entry.get("protocol"), nested.get("protocol"), config.get("protocol")) or "serial"
    return {
        "address": address,
        "label": label,
        "protocol": protocol,
        "vid": pick(identification.get("vid"), entry.get("vid")),
        "pid": pick(identification.get("pid"), entry.get("pid")),
        "serial_number": pick(identification.get("serialNumber"), entry.get("serial_number")),
        "hwid": pick(entry.get("hwid"), identification.get("hardwareId")),
    }


def _first_board(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    for key in ("boards",):
        value = entry.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    config = entry.get("config")
    if isinstance(config, dict):
        value = config.get("boards")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return None


def _parse_core(entry: Any) -> CoreInfo:
    if not isinstance(entry, dict):
        return CoreInfo(name=str(entry))
    library = entry.get("library") if isinstance(entry.get("library"), dict) else {}
    name = str(entry.get("name") or entry.get("id") or library.get("name") or "")
    version = str(entry.get("version") or entry.get("installed") or library.get("version") or "")
    latest = str(entry.get("latest") or "")
    if not latest:
        latest_entry = entry.get("latest")
        if isinstance(latest_entry, dict):
            latest = str(latest_entry.get("version") or "")
    boards = []
    for board in entry.get("boards") or []:
        if isinstance(board, dict):
            boards.append(str(board.get("name") or board.get("fqbn") or ""))
        else:
            boards.append(str(board))
    return CoreInfo(
        name=name,
        version=version,
        status=str(entry.get("status") or ("installed" if version else "")),
        latest=latest,
        boards=[b for b in boards if b],
        install_dir=str(entry.get("install_dir") or library.get("install_dir") or ""),
    )
