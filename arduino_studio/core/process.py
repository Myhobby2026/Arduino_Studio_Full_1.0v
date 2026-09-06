"""Subprocess plumbing shared by the Arduino CLI, library and flashing services.

Every external program is started through this module so that the app behaves
consistently:

* no console window flashes on Windows (``CREATE_NO_WINDOW``),
* ``stdout`` and ``stderr`` are merged, decoded as UTF-8 with replacement and
  stripped of ANSI colours before they reach the UI,
* the child process is registered with the running :class:`TaskContext`, so
  *Cancel* really kills it, and
* JSON emitting commands fall back to a tolerant parser when the CLI writes
  banner text around the payload.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .utils import ANSI_ESCAPE_RE, create_no_window_flag, get_logger, is_windows, strip_ansi

__all__ = [
    "CommandResult",
    "CommandError",
    "run_streaming",
    "run_capture",
    "run_json",
    "find_executable",
    "cli_search_dirs",
    "build_env",
    "terminate_process",
]

LOGGER = get_logger("process")


class CommandError(RuntimeError):
    """Raised when a command could not be started at all (missing binary, ...)."""

    def __init__(self, message: str, command: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.command = list(command or [])


@dataclass
class CommandResult:
    """Result of a finished subprocess."""

    command: list[str]
    returncode: int
    output: str
    duration: float
    cancelled: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled and not self.timed_out

    @property
    def lines(self) -> list[str]:
        return self.output.splitlines()

    def contains(self, *needles: str) -> bool:
        lowered = self.output.lower()
        return any(needle.lower() in lowered for needle in needles)

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ok


def build_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Copy of ``os.environ`` with toolchain-friendly additions."""
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["NO_COLOR"] = "1"
    env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def cli_search_dirs() -> list[Path]:
    """Folders that commonly contain ``arduino-cli`` on Windows (and elsewhere)."""
    dirs: list[Path] = []
    if is_windows():
        for var in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE", "USERPROFILE"):
            base = os.environ.get(var)
            if not base:
                continue
            root = Path(base)
            dirs += [
                root / "ArduinoCLI",
                root / "Programs" / "Arduino CLI",
                root / "Arduino" / "arduino-cli",
                root / "scoop" / "shims",
                root / "scoop" / "apps" / "arduino-cli" / "current",
                root / "bin",
                root / ".arduino15",
            ]
        for prog in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            if prog:
                dirs += [Path(prog) / "Arduino CLI", Path(prog) / "Arduino", Path(prog) / "arduino-cli"]
    else:
        home = Path.home()
        dirs += [
            Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt/arduino-cli"),
            home / ".local" / "bin", home / "bin", home / ".arduino15",
        ]
    if Path(sys.executable).parent.exists():
        dirs.append(Path(sys.executable).parent)
    out: list[Path] = []
    for item in dirs:
        try:
            if item.is_dir() and item not in out:
                out.append(item)
        except OSError:  # pragma: no cover - weird mount points
            continue
    return out


def find_executable(name: str, extra_dirs: Iterable[os.PathLike[str] | str] = ()) -> str:
    """Locate an executable by name, searching PATH plus well known folders."""
    exe_names = [name]
    if is_windows() and not name.lower().endswith((".exe", ".bat", ".cmd")):
        exe_names = [name + ".exe", name, name + ".bat", name + ".cmd"]
    for candidate in exe_names:
        found = shutil.which(candidate)
        if found:
            return found
    suffix = ".exe" if is_windows() else ""
    searched: list[Path] = [Path(p).expanduser() for p in extra_dirs]
    searched += cli_search_dirs()
    for folder in searched:
        for stem in exe_names:
            candidate = folder / stem
            if candidate.is_file():
                return str(candidate)
        if suffix:
            candidate = folder / (name + suffix)
            if candidate.is_file():
                return str(candidate)
    # last resort: recursive search in the user's arduino folders (shallow)
    for folder in _shallow_roots():
        for root, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d.lower() not in {"app-data", "packages", ".git", "cache"}]
            for filename in filenames:
                if filename.lower() in {n.lower() for n in exe_names} or filename.lower() == (name + suffix).lower():
                    return str(Path(root) / filename)
    return ""


def _shallow_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    for candidate in (
        home / ".arduino15", home / "AppData" / "Local" / "ArduinoCLI",
        home / "AppData" / "Local" / "Programs", home / "Arduino",
        Path(home) / "Downloads",
    ):
        try:
            if candidate.is_dir():
                roots.append(candidate)
        except OSError:  # pragma: no cover
            continue
    return roots


def terminate_process(process: "subprocess.Popen[Any]") -> None:
    """Terminate *process*, escalating to kill after 2 seconds."""
    try:
        if process.poll() is not None:
            return
        if is_windows():
            # CTRL_BREAK_EVENT-less kill: terminate() is fine for CLI tools.
            process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
    except OSError:
        LOGGER.debug("terminate failed", exc_info=True)


def _popen(command: Sequence[str], cwd: os.PathLike[str] | str | None, env: dict[str, str],
           stdin: int = subprocess.DEVNULL) -> "subprocess.Popen[Any]":
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "stdin": stdin,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
        "universal_newlines": False,
    }
    if is_windows():
        kwargs["creationflags"] = create_no_window_flag()
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(command), **kwargs)  # noqa: S603 - caller supplies the argv


def run_streaming(
    command: Sequence[str],
    *,
    cwd: os.PathLike[str] | str | None = None,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    context: Any = None,
    on_line: Optional[Callable[[str], Any]] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    """Run *command*, streaming merged output line by line.

    ``context`` (a :class:`~arduino_studio.core.runner.TaskContext`) is optional
    and used for cancellation plus return-code propagation.
    """
    argv = [str(part) for part in command]
    LOGGER.debug("exec: %s", " ".join(argv))
    started = time.perf_counter()
    try:
        process = _popen(argv, cwd, build_env(env))
    except FileNotFoundError as exc:
        raise CommandError(f"Cannot start '{argv[0]}': {exc}", argv) from exc
    except OSError as exc:
        raise CommandError(f"Cannot start '{argv[0]}': {exc}", argv) from exc

    if context is not None:
        try:
            context.attach(process)
        except Exception:  # pragma: no cover
            pass

    collected: list[str] = []
    write_error: list[BaseException] = []

    def _feed_stdin() -> None:
        if process.stdin is None:
            return
        try:
            data = (input_text or "").encode("utf-8", "replace")
            process.stdin.write(data)
            process.stdin.flush()
        except (OSError, ValueError) as exc:  # broken pipe when the tool exits early
            write_error.append(exc)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    if input_text is not None:
        threading.Thread(target=_feed_stdin, name="ardu-stdin", daemon=True).start()
    elif process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass

    def _reader() -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            if not raw:
                break
            text = strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\r\n")
            collected.append(text)
            if on_line is not None:
                try:
                    on_line(text)
                except Exception:  # pragma: no cover - a bad callback must not hang the child
                    LOGGER.exception("on_line callback failed")
        try:
            stream.close()
        except OSError:
            pass

    reader = threading.Thread(target=_reader, name="ardu-reader", daemon=True)
    reader.start()

    cancelled = False
    timed_out = False
    try:
        if timeout:
            deadline = started + timeout
            while process.poll() is None:
                if context is not None and getattr(context, "cancelled", False):
                    cancelled = True
                    terminate_process(process)
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    terminate_process(process)
                    break
                time.sleep(0.05)
        while process.poll() is None:
            if context is not None and getattr(context, "cancelled", False) and not cancelled:
                cancelled = True
                terminate_process(process)
                break
            time.sleep(0.05)
    finally:
        reader.join(timeout=5.0)
        returncode = process.poll()
        if returncode is None:
            try:
                returncode = process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                returncode = -1
        if context is not None:
            try:
                context.detach(process)
                context.set_returncode(returncode)
            except Exception:  # pragma: no cover
                pass
        if write_error:
            LOGGER.debug("stdin write failed: %s", write_error[0])

    output = "\n".join(collected)
    if cancelled and output and "cancelled" not in output.lower():
        output = output + "\n(error) command cancelled"
    if timed_out:
        output = (output + "\n" if output else "") + f"(error) timed out after {timeout:.0f} s"
    return CommandResult(
        command=argv,
        returncode=returncode if returncode is not None else -1,
        output=output,
        duration=time.perf_counter() - started,
        cancelled=cancelled,
        timed_out=timed_out,
    )


def run_capture(
    command: Sequence[str],
    *,
    cwd: os.PathLike[str] | str | None = None,
    env: Optional[dict[str, str]] = None,
    timeout: float = 25.0,
) -> CommandResult:
    """Run *command* and capture merged output (no streaming)."""
    argv = [str(part) for part in command]
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - caller supplies argv
            argv,
            cwd=str(cwd) if cwd else None,
            env=build_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            input=b"",
            creationflags=create_no_window_flag() if is_windows() else 0,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Cannot start '{argv[0]}': {exc}", argv) from exc
    except subprocess.TimeoutExpired as exc:
        partial = (exc.output or b"").decode("utf-8", errors="replace")
        return CommandResult(argv, -1, partial + f"\n(error) timed out after {timeout:.0f} s",
                             time.perf_counter() - started, timed_out=True)
    except OSError as exc:
        raise CommandError(f"Cannot start '{argv[0]}': {exc}", argv) from exc
    text = strip_ansi((completed.stdout or b"").decode("utf-8", errors="replace"))
    return CommandResult(argv, completed.returncode, text, time.perf_counter() - started)


def run_json(
    command: Sequence[str],
    *,
    cwd: os.PathLike[str] | str | None = None,
    timeout: float = 60.0,
    default: Any = None,
) -> tuple[Any, CommandResult]:
    """Run a ``--format json`` command; returns ``(parsed_or_default, result)``.

    Tolerates CLIs that print warnings before/after the JSON payload.
    """
    result = run_capture(command, cwd=cwd, timeout=timeout)
    return parse_json_output(result.output, default=default), result


def parse_json_output(text: str, default: Any = None) -> Any:
    """Extract the first JSON document embedded in *text*."""
    if not text:
        return default
    cleaned = strip_ansi(text).strip()
    if not cleaned:
        return default
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        end = cleaned.rfind(closer)
        if end <= start:
            continue
        candidate = cleaned[start:end + 1]
        for attempt in (candidate, _fix_json(candidate)):
            try:
                return json.loads(attempt)
            except ValueError:
                continue
    return default


def _fix_json(text: str) -> str:
    """Repair the two artefacts we actually see in the wild: trailing commas
    and ``//`` line comments printed by some tools."""
    without_comments = "\n".join(
        line for line in text.splitlines() if not ANSI_ESCAPE_RE.sub("", line).lstrip().startswith("//")
    )
    return re_sub_trailing_commas(without_comments)


def re_sub_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def process_alive(pid: int) -> bool:  # pragma: no cover - utility
    """Best effort "is this pid still alive" check."""
    try:
        os.kill(pid, 0)
    except (OSError, AttributeError):
        return False
    return True


def wait_for_file_stable(path: os.PathLike[str] | str, timeout: float = 5.0, interval: float = 0.2) -> bool:
    """Wait until *path* stops changing size (used after tool invocations)."""
    target = Path(path)
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            size = target.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        if size == last:
            return True
        last = size
        time.sleep(interval)
    return last >= 0
