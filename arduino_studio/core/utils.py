"""Small, dependency free helpers shared by every layer of Arduino Studio.

The module is intentionally free of any GUI import so it can be used from
worker threads, unit tests and command line tooling.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

__all__ = [
    "ANSI_ESCAPE_RE",
    "WINDOWS_FORBIDDEN",
    "WINDOWS_RESERVED_NAMES",
    "LOG_FILE_NAME",
    "setup_logging",
    "get_logger",
    "app_config_dir",
    "app_data_dir",
    "is_windows",
    "is_macos",
    "create_no_window_flag",
    "sanitize_name",
    "is_valid_project_name",
    "natural_key",
    "human_bytes",
    "human_duration",
    "strip_ansi",
    "Debouncer",
    "now_stamp",
    "iso_now",
    "parse_iso",
    "VersionTimer",
    "Debouncer",
    "run_revealed",
    "open_path_in_file_manager",
    "atomic_write_text",
    "safe_relpath",
    "iter_source_files",
    "ensure_dir",
    "copy_tree",
    "compare_versions",
]

LOG_FILE_NAME = "arduino_studio.log"

_ANSI_CONFIGURED = False
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

_ANSI_ESCAPE_RE = _ANSI_ESCAPE_RE  # exported alias
ANSI_ESCAPE_RE = _ANSI_ESCAPE_RE

SOURCE_SUFFIXES = (".ino", ".h", ".hpp", ".cpp", ".c", ".txt", ".json")


# --------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------
def setup_logging(
    log_dir: os.PathLike[str] | str | None = None, level: int = logging.INFO
) -> logging.Logger:
    """Configure the package logger, writing to *log_dir* when given.

    Calling this function twice is harmless: handlers are only added once and
    the second call merely adjusts the log level / log file location.
    """
    global _ANSI_CONFIGURED  # noqa: WPS420
    logger = logging.getLogger("arduino_studio")
    logger.setLevel(level)
    logger.propagate = False

    if _ANSI_CONFIGURED and log_dir is None:
        return logger

    if not logger.handlers:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        logger.addHandler(stream)

    if log_dir is not None:
        target_dir = Path(log_dir).expanduser()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("cannot create log directory %s", target_dir)
        else:
            log_path = target_dir / LOG_FILE_NAME
            if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
                try:
                    file_handler = RotatingFileHandler(
                        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
                    )
                    file_handler.setFormatter(
                        logging.Formatter(
                            "%(asctime)s %(levelname)-7s %(name)s.%(funcName)s:%(lineno)d: %(message)s"
                        )
                    )
                    logger.addHandler(file_handler)
                    _ANSI_CONFIGURED = True
                except OSError as exc:  # pragma: no cover - depends on FS permissions
                    logger.warning("cannot write log file %s: %s", log_path, exc)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return the child logger ``arduino_studio.<name>``."""
    return logging.getLogger(f"arduino_studio.{name}")


# --------------------------------------------------------------------------------------
# platform helpers
# --------------------------------------------------------------------------------------
def is_windows() -> bool:
    """True when running on Windows."""
    return os.name == "nt"


def is_macos() -> bool:
    """True when running on macOS."""
    return sys.platform == "darwin"


def create_no_window_flag() -> int:
    """``CREATE_NO_WINDOW`` on Windows, ``0`` elsewhere.

    Used for every ``subprocess`` call so that no console window flashes when
    the app runs from ``pythonw.exe`` or a PyInstaller ``--noconsole`` build.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def app_config_dir() -> Path:
    """Directory holding ``settings.json`` (``%APPDATA%\\ArduinoStudio`` on Windows)."""
    override = os.environ.get("ARDUINO_STUDIO_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_windows():
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
        if base:
            return Path(base) / "ArduinoStudio"
    elif is_macos():
        return Path.home() / "Library" / "Application Support" / "ArduinoStudio"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "arduino-studio"
    return Path.home() / ".config" / "arduino-studio"


def app_data_dir() -> Path:
    """Directory for caches, logs and other mutable application data."""
    override = os.environ.get("ARDUINO_STUDIO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if base:
            return Path(base) / "ArduinoStudio"
    elif is_macos():
        return Path.home() / "Library" / "Application Support" / "ArduinoStudio"
    data = os.environ.get("XDG_DATA_HOME", "").strip()
    if data:
        return Path(data) / "arduino-studio"
    return Path.home() / ".local" / "share" / "arduino-studio"


def ensure_dir(path: os.PathLike[str] | str) -> Path:
    """Create *path* (and parents) if needed and return it as :class:`Path`."""
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def run_detached(command: Sequence[str] | str, cwd: os.PathLike[str] | str | None = None) -> None:
    """Launch *command* without waiting for it (used for "open folder" actions)."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0  # type: ignore[attr-defined]
    subprocess.Popen(  # noqa: S603 - arguments are built internally
        command if isinstance(command, (list, tuple)) else [command],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def open_path_in_file_manager(path: os.PathLike[str] | str) -> bool:
    """Open the folder containing *path* in Explorer / Finder / file manager.

    Returns ``True`` when a launcher was found and started.
    """
    target = Path(path).expanduser()
    try:
        if is_windows():
            if not target.exists():
                return False
            subprocess.Popen(  # noqa: S603
                ["explorer", "/select,", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=create_no_window_flag(),
            )
            return True
        folder = target if target.is_dir() else (target.parent if target.parent != Path("") else target)
        if is_macos():
            subprocess.Popen(["open", "-R", str(target)], stdout=subprocess.DEVNULL)  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL)  # noqa: S603
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------------------
def sanitize_name(name: str, fallback: str = "ArduinoProject") -> str:
    """Make *name* safe for use as a file / project name.

    Arduino (and the C toolchain) tolerate only a small character set, so we
    keep letters, digits, dashes, dots and underscores.  Leading digits and
    reserved Windows names are prefixed/suffixed to stay usable.
    """
    cleaned = re.sub(r"[^\w.\-]+", "_", (name or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        return fallback
    # Windows reserved device names (case-insensitive, with or without extension)
    stem = cleaned.split(".", 1)[0].upper()
    reserved = {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6",
        "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
        "LPT7", "LPT8", "LPT9",
    }
    if stem in reserved:
        cleaned = f"_{cleaned}"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned[:80]


#: characters Windows refuses in a file or folder name (plus control codes)
WINDOWS_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: device names that cannot be used as a file or folder name on Windows
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def is_valid_project_name(name: str) -> tuple[bool, str]:
    """Validate a project name for the *New Project* dialog.

    The rules mirror what ``arduino-cli`` and Windows accept: the folder name
    becomes the sketch name, so characters the file system rejects (and the
    reserved device names) are refused here with a readable reason instead of a
    cryptic ``os.makedirs`` error later.
    """
    if not name or not name.strip():
        return False, "The project name cannot be empty."
    name = name.strip()
    if len(name) > 64:
        return False, "Keep the project name shorter than 64 characters."
    if WINDOWS_FORBIDDEN.search(name):
        return False, 'A name cannot contain \\ / : * ? " < > | or control characters.'
    if re.fullmatch(r"[\w.\- ]+", name, flags=re.UNICODE) is None:
        return False, "Use only letters, numbers, spaces, dashes, dots and underscores."
    if name[0].isdigit():
        return False, "A project name must not start with a digit."
    if name.endswith("."):
        return False, "A project name must not end with a dot."
    if name in (".", ".."):
        return False, "Invalid project name."
    stem = name.split(".")[0].strip().lower()
    if stem in WINDOWS_RESERVED_NAMES:
        return False, f"'{stem}' is a reserved Windows device name - choose another name."
    return True, ""


def natural_key(text: str) -> list[Any]:
    """Sort key that keeps ``file2.ino`` before ``file10.ino``."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences (arduino-cli colours them)."""
    if "\x1b" not in text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def human_bytes(num: float) -> str:
    """``3072`` -> ``"3.0 KB"``."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:,.1f} TB"


def human_duration(seconds: float) -> str:
    """Format an elapsed time in a human readable way (``"1 min 2.345 s"``)."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.3f} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)} min {rest:.3f} s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)} h {int(minutes)} min {rest:.0f} s"


def now_stamp() -> str:
    """Timestamp used for serial monitor / console lines (``"14:03:21.118"``)."""
    return datetime.now().strftime("%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"


def iso_now() -> str:
    """Current local time as an ISO-8601 string (second precision)."""
    return datetime.now().replace(microsecond=0).isoformat()


def parse_iso(text: str) -> Optional[datetime]:
    """Best effort ISO-8601 parser; returns ``None`` on bad input."""
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def compare_versions(left: str, right: str) -> int:
    """Compare dotted version strings, tolerating ``v`` prefixes and suffixes.

    Returns ``-1``, ``0`` or ``1`` like the old ``cmp()`` builtin.
    """

    def parts(value: str) -> list[int]:
        out: list[int] = []
        for chunk in re.split(r"[.\-_]", (value or "").strip().lstrip("vV")):
            match = re.match(r"(\d+)", chunk)
            out.append(int(match.group(1)) if match else 0)
        return out or [0]

    a, b = parts(left), parts(right)
    size = max(len(a), len(b))
    a += [0] * (size - len(a))
    b += [0] * (size - len(b))
    for x, y in zip(a, b):
        if x != y:
            return -1 if x < y else 1
    return 0


# --------------------------------------------------------------------------------------
# file helpers
# --------------------------------------------------------------------------------------
def atomic_write_text(path: os.PathLike[str] | str, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically (via a temp file + replace)."""
    target = Path(path).expanduser()
    ensure_dir(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    newline = "\r\n" if is_windows() else "\n"
    with open(tmp, "w", encoding=encoding, newline=newline) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp, target)
    except OSError:  # pragma: no cover - Windows can hold a lock briefly
        time.sleep(0.15)
        os.replace(tmp, target)


def safe_relpath(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> str:
    """Relative path of *path* to *root* using forward slashes (``/`` separators)."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).name


def iter_source_files(root: os.PathLike[str] | str, suffixes: Iterable[str] = SOURCE_SUFFIXES) -> Iterator[Path]:
    """Yield all editable source files below *root* (skipping hidden dirs)."""
    root_path = Path(root).expanduser()
    wanted = tuple(s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes)
    if not root_path.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")) and d not in {"build", "Debug", ".git"}
                              and not d.endswith(".bak"))
        for name in sorted(filenames):
            if name.endswith(wanted):
                yield Path(dirpath) / name


def copy_tree(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> int:
    """Recursively copy *src* into *dst*; returns the number of copied files."""
    src_path, dst_path = Path(src).expanduser(), Path(dst).expanduser()
    ensure_dir(dst_path)
    count = 0
    for item in sorted(src_path.rglob("*")):
        if item.name.startswith("."):
            continue
        rel = item.relative_to(src_path)
        target = dst_path / rel
        if item.is_dir():
            ensure_dir(target)
        else:
            ensure_dir(target.parent)
            target.write_bytes(item.read_bytes())
            count += 1
    return count


# --------------------------------------------------------------------------------------
# timing / debouncing helpers
# --------------------------------------------------------------------------------------
@dataclass
class VersionTimer:
    """Track the elapsed time of a long operation."""

    started_at: float = field(default_factory=time.perf_counter)

    def reset(self) -> None:
        self.started_at = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def text(self) -> str:
        return human_duration(self.elapsed)


class Debouncer:
    """Collapse bursty calls into a single delayed invocation.

    ``scheduler`` and ``canceller`` are provided by the caller (in the GUI they
    are ``widget.after`` and ``widget.after_cancel``) which keeps this class
    free of any Tkinter import, so it is also usable from worker threads.
    """

    def __init__(
        self,
        delay_ms: int,
        action: Callable[[], Any],
        scheduler: Callable[[int, Callable[[], Any]], Any],
        canceller: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._delay_ms = int(delay_ms)
        self._action = action
        self._scheduler = scheduler
        self._canceller = canceller
        self._id: Any = None
        self._lock = threading.Lock()

    def hit(self) -> None:
        """(Re)schedule the pending callback."""
        with self._lock:
            pending = self._id
            self._id = object()  # placeholder, replaced right below
        if pending is not None and self._canceller is not None:
            try:
                self._canceller(pending)
            except Exception:  # pragma: no cover - Tk may reject a stale id
                pass
        handle = self._scheduler(self._delay_ms, self._fire)
        with self._lock:
            self._id = handle if handle is not None else self._id

    def flush(self) -> None:
        """Run the pending action immediately when one is scheduled."""
        with self._lock:
            pending = self._id is not None
            self._id = None
        if pending:
            self._action()

    def cancel(self) -> None:
        """Drop any pending call."""
        with self._lock:
            pending = self._id
            self._id = None
        if pending is not None and self._canceller is not None:
            try:
                self._canceller(pending)
            except Exception:  # pragma: no cover
                pass

    @property
    def pending(self) -> bool:
        return self._id is not None

    def _fire(self) -> None:
        with self._lock:
            self._id = None
        try:
            self._action()
        except Exception:  # never let a deferred UI job kill the mainloop
            get_logger("utils").exception("debounced action failed")


def clamp(value: float, low: float, high: float) -> float:
    """Return *value* limited to ``[low, high]``."""
    return max(low, min(high, value))


def split_lines(text: str) -> list[str]:
    """Split *text* into display lines, dropping the trailing empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
