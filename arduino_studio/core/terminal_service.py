"""Integrated terminal backend.

A real, persistent shell (PowerShell by default, ``cmd.exe`` / ``bash`` as
alternatives) is kept alive as a child process.  Every command is followed by a
*sentinel* line printed by the shell itself, which is how Arduino Studio knows

* when a command has finished (``exit status`` in the status line),
* which directory the shell is in now (``cd`` works as usual), and
* whether the shell considered the command successful.

The terminal is intentionally independent from the serial monitor: it runs
operating system commands, it never touches a COM port.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .utils import create_no_window_flag, get_logger, is_windows, strip_ansi

__all__ = [
    "CommandRecord",
    "DESTRUCTIVE_PATTERNS",
    "SHELLS",
    "TerminalError",
    "TerminalService",
    "TerminalShell",
    "is_destructive",
    "quick_commands",
]

SENTINEL = "__ARDUINO_STUDIO_END__"

#: ``SENTINEL|marker|exit|ok|cwd``
SENTINEL_RE = re.compile(re.escape(SENTINEL) + r"\|(?P<marker>\d+)\|(?P<exit>-?\d+)\|(?P<ok>\d+)\|(?P<cwd>.*)$")


@dataclass(frozen=True)
class TerminalShell:
    """Launch parameters and shell specific idioms."""

    key: str
    label: str
    argv: tuple[str, ...]
    executable: str
    clear_command: str
    exit_sentinel: str          # fragment that expands the exit code
    cwd_sentinel: str           # fragment that expands the working directory
    ok_sentinel: str = "1"      # 1 == "shell reports success"

    def launch_args(self) -> list[str]:
        return list(self.argv)

    def sentinel_line(self, marker: int) -> str:
        """The echo command that marks the end of command *marker*.

        ``cmd.exe`` treats ``|`` as a pipe, so the separator is escaped with
        ``^`` there; the shell still prints a plain ``|`` which is what
        :data:`SENTINEL_RE` matches.
        """
        sep = self.sep
        raw = f"{SENTINEL}{sep}{marker}{sep}{self.exit_sentinel}{sep}{self.ok_sentinel}{sep}{self.cwd_sentinel}"
        if self.key == "powershell":
            return f'Write-Output "{raw}"'
        if self.key == "bash":
            return f'echo "{raw}"'
        return f"echo {raw}"

    @property
    def sep(self) -> str:
        """Field separator, escaped for ``cmd.exe`` (where ``|`` pipes)."""
        return "^|" if self.key == "cmd" else "|"


POWERSHELL = TerminalShell(
    key="powershell",
    label="PowerShell",
    argv=("powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"),
    executable="powershell.exe",
    clear_command="Clear-Host",
    # $LASTEXITCODE is only meaningful for external programs; fall back to 0.
    exit_sentinel="$(__as_exit)",
    cwd_sentinel="$((Get-Location).Path)",
    ok_sentinel="$(__as_ok)",
)

CMD = TerminalShell(
    key="cmd",
    label="Command Prompt",
    argv=("cmd.exe", "/Q", "/V:ON", "/K"),
    executable="cmd.exe",
    clear_command="cls",
    exit_sentinel="!ERRORLEVEL!",
    cwd_sentinel="!CD!",
    ok_sentinel="1",
)

BASH = TerminalShell(
    key="bash",
    label="Bash",
    argv=("bash", "--noediting", "-i"),
    executable="bash",
    clear_command="clear",
    exit_sentinel="$?",
    cwd_sentinel="$PWD",
    ok_sentinel="1",          # the exit code already says everything for bash
)

#: Every shell Arduino Studio can spawn (the UI offers the usable ones).
SHELLS: dict[str, TerminalShell] = {shell.key: shell for shell in (POWERSHELL, CMD, BASH)}

#: PowerShell needs a helper preamble so that the sentinel knows the exit code of
#: a *cmdlet* as well as of external programs.
POWERSHELL_PREAMBLE = (
    "$ErrorActionPreference='Continue'; "
    "function global:__as_ok_marker {}; "
)

#: Commands that ask for an explicit confirmation before running.
DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    r"\bformat(?:\.com)?\b\s+[a-z]?:",
    r"\bdiskpart\b",
    r"\bdel\b\s+/[fq]",
    r"\berase\b\s",
    r"remove-item\b[^\n]*-(recurse|force)",
    r"\brm\b\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|-r\b|-rf\b|-fr\b|--recursive)",
    r"\brmdir\b\s+/s",
    r"\brd\b\s+/s",
    r"\bsudo\b",
    r"\bdd\b\s+if=",
    r"\bmkfs(?:\.\w+)?\b",
    r">\\\\\.\\",
    r"\breg\s+(delete|add)\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\btaskkill\b\s+/f",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bchmod\s+-r\b",
    r"\bicacls\b.*\/grant",
    r"\bcd\.exe\b",
    r"\biperun\s+(delete|reset)\b",
    r"\bdel\s+/q\s+/s",
    r"\bdel\b\s+\*\.\w+\s+/s",
    r"\bRemove-Item\b.*\\\\\.\\",
    r"\bcurl\b.*\|\s*(sh|bash)",
    r"\bwmic\b.*delete",
)
_DESTRUCTIVE_REGEXES = tuple((pattern, re.compile(pattern, re.IGNORECASE)) for pattern in DESTRUCTIVE_PATTERNS)


class TerminalError(RuntimeError):
    """Raised when the shell cannot be started or used."""


@dataclass
class CommandRecord:
    """One executed command with its outcome (used by the history list)."""

    command: str
    returncode: Optional[int] = None
    ok: Optional[bool] = None
    started: float = field(default_factory=time.perf_counter)
    duration: float = 0.0
    cwd: str = ""

    @property
    def status(self) -> str:
        if self.returncode is None:
            return "running"
        if self.returncode == 0 and self.ok is not False:
            return "exit 0 (ok)"
        return f"exit {self.returncode}"


def is_destructive(command: str) -> tuple[bool, str]:
    """Detect commands that can destroy data (used for the safety prompt)."""
    text = (command or "").strip()
    if not text:
        return False, ""
    lowered = text.lower()
    for pattern, regex in _DESTRUCTIVE_REGEXES:
        if regex.search(lowered):
            return True, _describe_pattern(pattern)
    return False, ""


def _describe_pattern(pattern: str) -> str:
    """Turn a regex into a readable warning."""
    readable = {
        r"\bformat(?:\.com)?\b\s+[a-z]?:": "formatting a drive erases everything on it",
        r"\bdiskpart\b": "diskpart repartitions physical disks",
        r"\bdel\b\s+/[fq]": "a forced recursive delete",
        r"remove-item\b[^\n]*-(recurse|force)": "a recursive Remove-Item",
        r"\brm\b\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|-r\b|-rf\b|-fr\b|--recursive)": "a recursive rm",
        r"\bsudo\b": "elevated privileges",
        r"\bgit\s+reset\s+--hard\b": "git reset --hard discards uncommitted changes",
        r"\bgit\s+clean\s+-[a-z]*f": "git clean removes untracked files",
        r"\btaskkill\b\s+/f": "force-killing processes",
        r"\bshutdown\b": "shutting down the computer",
        r"\breboot\b": "restarting the computer",
        r"\bdel\s+/q\s+/s": "deleting many files at once",
        r"\bcurl\b.*\|\s*(sh|bash)": "piping a download into a shell",
    }
    return readable.get(pattern, "this command can modify or delete files outside the project")


def _which(name: str) -> str:
    from .process import find_executable

    return find_executable(name)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class TerminalService:
    """Drive a persistent shell, streaming its output through callbacks.

    Callbacks (invoked in the UI thread when ``ui_post`` is given):

    ``on_output(kind, text)``
        ``kind`` is one of ``output``, ``command``, ``info``, ``error``, ``echo``.
    ``on_prompt(cwd, exit_code, ok)``
        Emitted after each command with the shell's new location and status.
    """

    def __init__(
        self,
        ui_post: Optional[Callable[[Callable[[], Any]], Any]] = None,
        on_output: Optional[Callable[[str, str], Any]] = None,
        on_prompt: Optional[Callable[[str, Optional[int], Optional[bool]], Any]] = None,
        cwd: os.PathLike[str] | str | None = None,
        shell: str = "",
    ) -> None:
        self._log = get_logger("terminal")
        self._post = ui_post or (lambda fn: fn())
        self._on_output = on_output
        self._on_prompt = on_prompt
        self.cwd = str(Path(cwd).expanduser()) if cwd else os.getcwd()
        self.shell_key = shell if shell in SHELLS else self.default_shell_key()
        self.history: list[CommandRecord] = []
        self.history_limit = 200
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._reader: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._marker = 0
        self._current: Optional[CommandRecord] = None
        self._last_exit: Optional[int] = None
        self._last_ok: Optional[bool] = None
        self._alive = threading.Event()
        self._auto_restart = True
        self._restarts = 0

    # ------------------------------------------------------------- discovery
    @staticmethod
    def default_shell_key() -> str:
        """The shell that exists on this machine (PowerShell on Windows)."""
        if is_windows():
            if _which("powershell.exe"):
                return POWERSHELL.key
            return CMD.key
        return BASH.key if _which("bash") else CMD.key

    @staticmethod
    def available_shells() -> list[tuple[str, str, bool]]:
        """``[(key, label, usable)]`` for the settings screen."""
        rows: list[tuple[str, str, bool]] = []
        for key, shell in SHELLS.items():
            usable = bool(_which(shell.executable))
            if not is_windows() and key in {POWERSHELL.key, CMD.key}:
                usable = False
            rows.append((key, shell.label, usable))
        rows.sort(key=lambda row: (not row[2], row[1]))
        return rows

    @property
    def shell(self) -> TerminalShell:
        return SHELLS.get(self.shell_key, POWERSHELL)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def last_exit_code(self) -> Optional[int]:
        return self._last_exit

    @property
    def last_ok(self) -> Optional[bool]:
        return self._last_ok

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def prompt_label(self) -> str:
        """Short label shown next to the input line (``PowerShell ~/Arduino/X``)."""
        cwd = self.cwd or os.getcwd()
        try:
            home = str(Path.home())
            if cwd.startswith(home):
                cwd = "~" + cwd[len(home):]
        except OSError:  # pragma: no cover
            pass
        return f"{self.shell.label} - {cwd}"

    # ------------------------------------------------------------- lifecycle
    def start(self, cwd: os.PathLike[str] | str | None = None, quiet: bool = False) -> None:
        """Launch the shell (does nothing when a session is already running)."""
        if self.running:
            if cwd and str(Path(cwd).expanduser()) != self.cwd:
                self.change_dir(str(Path(cwd).expanduser()))
            return
        if cwd:
            candidate = Path(cwd).expanduser()
            if candidate.is_dir():
                self.cwd = str(candidate)
        shell = self.shell
        if not _which(shell.executable):
            raise TerminalError(
                f"{shell.label} ('{shell.executable}') was not found. Choose another shell in "
                "Settings -> Terminal."
            )
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv comes from the fixed SHELLS table
                shell.launch_args(),
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=create_no_window_flag() if is_windows() else 0,
            )
        except OSError as exc:
            self._proc = None
            raise TerminalError(f"Cannot start {shell.label}: {exc}") from exc
        self._alive.set()
        self._auto_restart = True
        self._reader = threading.Thread(target=self._read_loop, name="ardu-terminal-reader", daemon=True)
        self._reader.start()
        if not quiet:
            self._emit("info", f"{shell.label} session started in {self.cwd}")
        if shell.key == POWERSHELL.key:
            # seed the helper variables the sentinel reads
            self._send_raw(
                "$__as_exit = 0\n$__as_ok = 1\n$ErrorActionPreference = 'Continue'\n"
                "$ProgressPreference = 'SilentlyContinue'\n"
            )
        self._send_raw(shell.sentinel_line(self._next_marker()) + "\n")

    def stop(self, wait: float = 1.2, silent: bool = False) -> None:
        """Close the shell session (``exit`` first, then terminate)."""
        self._auto_restart = False
        self._alive.clear()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"exit\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if not silent:
            self._emit("info", f"{self.shell.label} session ended.")

    def restart(self, cwd: os.PathLike[str] | str | None = None) -> None:
        """Kill and relaunch the shell (also used after a crash)."""
        target = str(Path(cwd).expanduser()) if cwd else self.cwd
        self.stop(wait=0.8, silent=True)
        self._marker = 0
        self.start(cwd=target)

    def set_shell(self, key: str) -> bool:
        """Switch the shell flavour; restarts the session when it changed."""
        if key not in SHELLS or key == self.shell_key:
            return False
        self.shell_key = key
        if self._proc is not None:
            self.restart()
        return True

    # -------------------------------------------------------------- commands
    def send_command(self, command: str) -> bool:
        """Write *command* to the shell followed by its sentinel line."""
        text = (command or "").strip("\r\n")
        if not self.running:
            try:
                self.start()
            except TerminalError as exc:
                self._emit("error", str(exc))
                return False
        if not text:
            return self._send_raw(self.shell.sentinel_line(self._next_marker()) + "\n")
        self._emit("command", text)
        marker = self._next_marker()
        record = CommandRecord(command=text, cwd=self.cwd)
        self._current = record
        self.history.append(record)
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]
        payload = self._wrap(text, marker)
        return self._send_raw(payload)

    def _wrap(self, text: str, marker: int) -> str:
        """Command + sentinel, glued so that ``$?``/``!ERRORLEVEL!`` stay valid."""
        shell = self.shell
        sentinel = shell.sentinel_line(marker)
        if shell.key == POWERSHELL.key:
            return (
                f"$out__ = & {{ {text} }} 2>&1; $__as_exit = $LASTEXITCODE; $__as_ok = [int]$?\n"
            ) if False else (
                f"{text}\n"
                f"$__as_exit = if ($null -ne $LASTEXITCODE) {{ $LASTEXITCODE }} else {{ 0 }}\n"
                f"$__as_ok = [int]$?\n"
                f"{sentinel}\n"
            )
        if shell.key == CMD.key:
            # single line: !ERRORLEVEL! (delayed expansion) refers to *text*
            return f"{text} & {sentinel}\n"
        return f"{text}\n{sentinel}\n"

    def change_dir(self, path: os.PathLike[str] | str) -> bool:
        """``cd`` inside the shell so the prompt, ``dir`` etc. follow the project."""
        target = Path(str(path)).expanduser()
        if self.shell.key == POWERSHELL.key:
            quoted = "'" + str(target).replace("'", "''") + "'"
            return self.send_command(f"Set-Location -LiteralPath {quoted}")
        if is_windows():
            return self.send_command(f'cd /d "{target}"')
        return self.send_command(f"cd '{str(target).replace(chr(39), chr(39) * 2)}'")

    def set_cwd_tracking_target(self, path: os.PathLike[str] | str) -> None:
        """Follow the current project (called when the open project changes)."""
        target = Path(str(path)).expanduser()
        if not target.is_dir():
            return
        if self.running:
            if str(target) != self.cwd:
                self.change_dir(target)
        else:
            self.cwd = str(target)

    def clear_screen(self) -> bool:
        """Clear the shell screen (``Clear-Host`` / ``cls``)."""
        return self.send_command(self.shell.clear_command)

    def interrupt(self) -> bool:
        """Interrupt the running command.

        On POSIX a real ``SIGINT`` is delivered.  A piped shell cannot receive
        ``Ctrl+C``, so there the command is finished normally and *Break* only
        cancels Arduino Studio's one-shot helpers (see :meth:`stop`).
        """
        proc = self._proc
        if proc is None:
            return False
        if not is_windows():
            try:
                proc.send_signal(subprocess.SIGINT)
                return True
            except (OSError, AttributeError):  # pragma: no cover
                return False
        self._emit("info", "A piped shell cannot receive Ctrl+C - use 'Restart' to drop a stuck session.")
        return False

    def run_captured(self, command: str, timeout: float = 120.0) -> tuple[int, str]:
        """Run *command* in a **separate** process (does not disturb the shell)."""
        argv = self.split_command_line(command)
        if not argv:
            return 1, "Empty command"
        try:
            completed = subprocess.run(  # noqa: S603 - argv built from the user's input on purpose
                argv,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                input=b"",
                creationflags=create_no_window_flag() if is_windows() else 0,
            )
        except FileNotFoundError:
            return 127, f"Command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return 124, f"'{command}' timed out after {timeout:.0f} s"
        except OSError as exc:
            return 126, f"Cannot run '{command}': {exc}"
        return completed.returncode, (completed.stdout or b"").decode("utf-8", errors="replace")

    def submit_one_shot(
        self,
        command: str,
        context: Any = None,
        on_line: Optional[Callable[[str], Any]] = None,
    ) -> tuple[int, str]:
        """Streamed one-shot run, used by the quick action buttons."""
        from . import process as proc

        argv = self.split_command_line(command)
        if not argv:
            return 1, "Empty command"
        try:
            result = proc.run_streaming(
                argv,
                cwd=self.cwd,
                context=context,
                on_line=on_line or (lambda line: self._emit("output", line)),
            )
        except proc.CommandError as exc:
            return 127, str(exc)
        return result.returncode, result.output

    # ------------------------------------------------------------- internals
    def _next_marker(self) -> int:
        with self._write_lock:
            self._marker += 1
            return self._marker

    def _send_raw(self, payload: str) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            self._emit("error", "The terminal is not running - use Restart Terminal.")
            return False
        data = payload.encode("utf-8", errors="replace")
        if not data.endswith(b"\n"):
            data += b"\n"
        try:
            with self._write_lock:
                proc.stdin.write(data)
                proc.stdin.flush()
            return True
        except (OSError, ValueError) as exc:
            self._emit("error", f"Cannot write to the shell: {exc}")
            self._handle_death()
            return False

    def _handle_death(self) -> None:
        proc = self._proc
        code = proc.poll() if proc is not None else None
        if code is not None or proc is None:
            self._emit("info", f"Shell exited (code {code})." if code is not None else "Shell is not running.")
        if self._auto_restart:
            self._restarts += 1
            self._emit("info", "Restarting the terminal session...")
            try:
                self.restart()
            except TerminalError as exc:
                self._emit("error", str(exc))

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                text = strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\r\n")
                match = SENTINEL_RE.search(text)
                if match:
                    self._handle_sentinel(match)
                    continue
                if _is_shell_noise(text, self.shell.key):
                    continue
                self._emit("output", text)
        except (OSError, ValueError) as exc:
            self._log.debug("terminal reader stopped: %s", exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass
            if self._alive.is_set() and self._auto_restart and (proc.poll() if proc else None) is not None:
                self._post(lambda: self._handle_death())
            self._alive.clear()

    def _handle_sentinel(self, match: "re.Match[str]") -> None:
        code = _safe_int(match.group("exit"), 0)
        ok = _safe_int(match.group("ok"), 1) != 0
        cwd = (match.group("cwd") or "").strip().strip('"')
        self._last_exit = code
        self._last_ok = ok
        if cwd and os.path.isdir(cwd):
            self.cwd = cwd
        record = self._current
        if record is not None:
            record.returncode = code
            record.ok = ok
            record.duration = time.perf_counter() - record.started
            record.cwd = self.cwd
            self._current = None
            if code != 0 or not ok:
                self._emit("info", f"exit status {code}" + ("" if ok else " (shell reported an error)"))
        self._post(lambda: self._on_prompt and self._on_prompt(self.cwd, code, ok))

    def _emit(self, kind: str, text: str) -> None:
        callback = self._on_output
        if callback is None:
            return
        for line in str(text).split("\n"):
            self._post(lambda l=line: callback(kind, l))

    # -------------------------------------------------------------- helpers
    @staticmethod
    def split_command_line(command: str) -> list[str]:
        """Split a command line into an argv list (Windows quoting aware)."""
        text = (command or "").strip()
        if not text:
            return []
        try:
            return shlex.split(text, posix=not is_windows())
        except ValueError:
            return text.split()

    def recent_commands(self, limit: int = 20) -> list[str]:
        """Unique command strings, most recent first (for the Up-arrow history)."""
        out: list[str] = []
        for record in reversed(self.history):
            if record.command and record.command not in out:
                out.append(record.command)
            if len(out) >= limit:
                break
        return out


_PS_NOISE = (
    re.compile(r"^\s*PS [A-Za-z]:\\.*>\s*$"),
    re.compile(r"^\s*>>\s*$"),
    re.compile(r"^\s*\[Console\]::TreatControlCAsInput\s*=\s*\$false\s*$"),
)


def _is_shell_noise(text: str, shell_key: str) -> bool:
    """True for prompt/banner/echo lines we replace with our own UI label."""
    stripped = text.strip()
    if not stripped:
        return False
    if re.search(r"echo\s+[\"']?" + re.escape(SENTINEL), stripped):
        return True          # echoed input line in interactive shells
    if shell_key == POWERSHELL.key and any(rx.match(text) for rx in _PS_NOISE):
        return True
    if shell_key == CMD.key and re.match(r"^[A-Za-z]:\\[^>]*>$", text.strip()):
        return True
    if shell_key == BASH.key and re.match(r"^[\w@\-./~$#\[\]\\001\002]+[$#>]\s*(.*)$", stripped):
        return True          # "user@host:dir$" prompt prefix from interactive bash
    return False


def quick_commands(cli_path: str = "", project_dir: os.PathLike[str] | str = "") -> list[tuple[str, str]]:
    """Preset buttons for the terminal panel: ``[(label, command)]``.

    The command is inserted into the input line so the user can still edit it
    before pressing Enter.
    """
    cli = (cli_path or "").strip() or "arduino-cli"
    if cli and not Path(cli).is_file():
        cli = "arduino-cli.exe" if is_windows() else "arduino-cli"
    quoted = f'"{cli}"' if " " in cli else cli
    folder = str(Path(project_dir).expanduser()) if project_dir else ""
    items: list[tuple[str, str]] = [
        ("board list", f"{quoted} board list"),
        ("board listall", f"{quoted} board listall"),
        ("compile", f"{quoted} compile --fqbn arduino:avr:uno"),
        ("upload", f"{quoted} upload --port COM5 --fqbn arduino:avr:uno"),
        ("lib list", f"{quoted} lib list"),
        ("lib search", f"{quoted} lib search Servo"),
        ("lib install", f"{quoted} lib install \"DHT sensor library\""),
        ("cores", f"{quoted} core list"),
        ("update index", f"{quoted} core update-index"),
        ("cache clean", f"{quoted} cache clean"),
        ("version", f"{quoted} version"),
        ("list files", "dir" if is_windows() else "ls -la"),
    ]
    if folder:
        items.append(("to project", f'cd "{folder}"' if is_windows() else f"cd '{folder}'"))
        items.append(("delete build dir", "rmdir /s /q build" if is_windows() else "rm -rf build"))
    return items
