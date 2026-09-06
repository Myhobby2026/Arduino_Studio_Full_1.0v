"""Command line entry point for Arduino Studio.

Responsibilities:

* parse the command line (see :func:`build_parser`),
* start logging **before** the GUI exists, so a crash during start-up is still
  written to ``<config dir>/logs/arduino_studio.log``,
* install a :func:`sys.excepthook` that logs stray exceptions instead of
  silently killing the Tk main loop,
* hand over to :class:`~arduino_studio.ui.app.ArduinoStudioApp`, and
* provide ``--check`` - a headless self test that prints everything support
  needs (Python build, Arduino CLI location and version, serial backend,
  config folder) without opening a window.

Run it with ``python -m arduino_studio``, ``python run.py``, ``arduino-studio``
(the console script installed by ``pip install .``) or by double-clicking the
packaged ``ArduinoStudio.exe``.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .core.settings import SettingsStore
from .core.utils import LOG_FILE_NAME, get_logger, human_duration, setup_logging

__all__ = ["Options", "build_parser", "main", "parse_args", "run_self_check"]

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


# --------------------------------------------------------------------- options
@dataclass(frozen=True)
class Options:
    """Everything :func:`main` needs, independent of :mod:`argparse`."""

    project: Optional[Path] = None
    config_dir: Optional[Path] = None
    force_setup: bool = False
    skip_setup: bool = False
    log_level: str = "INFO"
    check: bool = False
    new_project: str = ""
    new_project_parent: Optional[Path] = None
    version: bool = False


class _ArgumentParser(argparse.ArgumentParser):
    """Parser that exits with a usable status code in a frozen build too."""

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # noqa: D102
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    """The command line interface of the app."""
    parser = _ArgumentParser(
        prog="arduino-studio",
        description="Arduino Studio - a standalone Arduino IDE that drives arduino-cli.",
        epilog=(
            "examples:\n"
            "  arduino-studio                       open the last project\n"
            "  arduino-studio Documents/Blink       open Documents/Blink\n"
            "  arduino-studio --new-project Sensor  create and open a project\n"
            "  arduino-studio --setup               show the first-run setup screen\n"
            "  arduino-studio --check               print a headless support report\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project", nargs="?", default="", metavar="PROJECT",
                        help="path to a project folder or a .ino file to open on start-up")
    parser.add_argument("--config-dir", metavar="DIR", default="",
                        help="folder for settings.json and logs (Windows default: %%APPDATA%%\\ArduinoStudio "
                             "on Windows, ~/.config/arduino-studio elsewhere; the environment variable "
                             "ARDUINO_STUDIO_CONFIG_DIR does the same)")
    parser.add_argument("--new-project", metavar="NAME", default="",
                        help="create a new project and open it")
    parser.add_argument("--project-parent", metavar="DIR", default="",
                        help="where --new-project should be created (default: your sketchbook/Documents)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true",
                      help="force the first-run setup screen (arduino-cli path, board manager URLs)")
    mode.add_argument("--no-setup", action="store_true",
                      help="never show the setup screen, even on a fresh profile")
    parser.add_argument("--log-level", default="INFO", choices=LOG_LEVELS,
                        help="verbosity of the log file (default: INFO)")
    parser.add_argument("--check", action="store_true",
                        help="run the headless self test and exit (no window is opened)")
    parser.add_argument("-V", "--version", action="store_true", help="print the version and exit")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> Options:
    """Turn ``argv`` (default: :data:`sys.argv`) into :class:`Options`."""
    raw = list(sys.argv[1:] if argv is None else argv)
    namespace = build_parser().parse_args(raw)

    def path_or_none(value: str) -> Optional[Path]:
        text = str(value or "").strip()
        return Path(text).expanduser() if text else None

    return Options(
        project=path_or_none(namespace.project),
        config_dir=path_or_none(namespace.config_dir),
        force_setup=bool(namespace.setup),
        skip_setup=bool(namespace.no_setup),
        log_level=str(namespace.log_level),
        check=bool(namespace.check),
        new_project=str(namespace.new_project or "").strip(),
        new_project_parent=path_or_none(namespace.project_parent),
        version=bool(namespace.version),
    )


# ------------------------------------------------------------------ self check
def run_self_check(options: Options) -> int:
    """Print a support report and return a shell exit code (0 = everything sane)."""
    from .core.arduino_cli import ArduinoCLI

    store = SettingsStore(options.config_dir)
    settings = store.load()
    print(f"Arduino Studio {__version__}")
    print(f"  started at      : {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  python          : {platform.python_version()} ({sys.executable})")
    print(f"  platform        : {platform.platform()}")
    print(f"  frozen build    : {bool(getattr(sys, 'frozen', False))}")
    print(f"  config folder   : {store.directory}")
    print(f"  settings.json   : {store.path} ({'present' if store.path.is_file() else 'not created yet'})")
    print(f"  log folder      : {store.log_dir}")
    print(f"  data folder     : {store.data_dir}")

    problems = 0

    cli_path = settings.effective_cli_path() or ArduinoCLI.auto_detect()
    if not cli_path:
        problems += 1
        print("  arduino-cli     : NOT FOUND - install it (see README) or set the path in Settings")
    else:
        cli = ArduinoCLI(cli_path=cli_path)
        info = cli.probe(cli_path)
        if info.ok:
            print(f"  arduino-cli     : {info.version} at {info.path}")
            if getattr(info, "data_dir", ""):
                print(f"  CLI data dir    : {info.data_dir}")
        else:
            problems += 1
            print(f"  arduino-cli     : found {cli_path} but it did not respond: {info.message}")

    try:  # serial backend
        import serial  # type: ignore[import-not-found]

        from .core.serial_service import list_serial_ports

        ports = list_serial_ports()
        print(f"  pyserial        : {getattr(serial, '__version__', 'unknown')}, "
              f"{len(ports)} port(s) visible: {', '.join(p.name for p in ports) or 'none'}")
    except Exception as exc:  # pragma: no cover - depends on the machine
        problems += 1
        print(f"  pyserial        : unavailable ({exc}) - the Serial Monitor will be disabled")

    try:  # GUI toolkit availability matters on Linux/macOS source installs
        import tkinter  # noqa: F401

        print(f"  tkinter         : available (Tk {tkinter.TkVersion}.{tkinter.TclVersion})")
        try:
            import customtkinter  # type: ignore[import-not-found]  # noqa: F401

            print("  customtkinter   : available")
        except Exception:
            print("  customtkinter   : NOT installed - run 'pip install customtkinter'")
    except Exception as exc:  # pragma: no cover - headless / no python3-tk
        problems += 1
        print(f"  tkinter         : unavailable ({exc}) - install python3-tk")

    from .core.boards import BUILTIN_BOARDS

    print(f"  board presets   : {', '.join(board.fqbn for board in BUILTIN_BOARDS)}")
    print(f"  exit status     : {'OK' if problems == 0 else f'{problems} problem(s) found'}")
    return 0 if problems == 0 else 1


# ------------------------------------------------------------------- excepthook
def _install_excepthook(log: logging.Logger) -> None:
    """Log stray exceptions instead of letting Tk die mid-edit.

    Tk calls back into Python from its own event loop; an exception there is
    printed by :data:`sys.excepthook` and the widget simply ignores the event,
    so the window stays alive.  Making that path visible in the log file is
    what turns "the button does nothing" into a diagnosable bug.
    """

    def hook(kind: type[BaseException], value: BaseException, tb: object) -> None:
        if kind is KeyboardInterrupt:
            print("Interrupted.")
            return
        text = "".join(traceback.format_exception(kind, value, tb))
        log.error("unhandled exception:\n%s", text.rstrip())
        sys.stderr.write(text)

    sys.excepthook = hook  # type: ignore[assignment]


def _bootstrap_logging(options: Options) -> logging.Logger:
    """Configure logging before any window exists (so start-up crashes are logged)."""
    store = SettingsStore(options.config_dir)
    level = getattr(logging, str(options.log_level).upper(), logging.INFO)
    setup_logging(log_dir=store.log_dir, level=int(level))
    return get_logger("main")


# ------------------------------------------------------------------------- main
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: parse the command line, start the GUI, return an exit code."""
    try:
        options = parse_args(argv)
    except SystemExit as exc:  # --help / a bad flag: already reported
        return int(exc.code or 0)

    if options.version:
        print(f"Arduino Studio {__version__}")
        return 0

    log = _bootstrap_logging(options)
    _install_excepthook(log)

    if options.check:
        return run_self_check(options)

    if options.new_project and not options.project:
        options = _create_requested_project(options, log)
        if options.project is None:
            return 2

    if options.project is not None and not options.project.exists():
        log.warning("ignoring '%s': no such file or folder", options.project)
        print(f"Note: '{options.project}' does not exist - opening without a project.", file=sys.stderr)
        options = replace(options, project=None)

    try:
        from .ui import create_app  # imported late: only the GUI needs Tk
    except Exception as exc:  # pragma: no cover - broken toolkit install
        log.exception("could not import the user interface")
        print(f"Arduino Studio could not start its interface: {exc}", file=sys.stderr)
        print("On Linux the Tkinter package is separate: sudo apt install python3-tk", file=sys.stderr)
        print("Also check 'python -m arduino_studio --check'.", file=sys.stderr)
        return 1

    started = datetime.now()
    app = None
    try:
        app = create_app(_argv_for_app(options))
        app.mainloop()
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C in a console build
        print("Interrupted.")
        return 130
    except Exception as exc:
        log.exception("fatal error while the window was open")
        _report_crash(exc, log, log_file=store_log_path(options))
        return 1
    finally:
        if app is not None:
            try:
                app.destroy()
            except Exception:  # pragma: no cover - window already gone
                pass
        elapsed = (datetime.now() - started).total_seconds()
        if elapsed > 2:
            log.info("session finished after %s", human_duration(elapsed))
    return 0


def _argv_for_app(options: Options) -> list[str]:
    """Re-serialise the parsed options for :func:`arduino_studio.ui.app.create_app`."""
    argv: list[str] = []
    if options.project is not None:
        argv.append(str(options.project))
    if options.config_dir is not None:
        argv += ["--config-dir", str(options.config_dir)]
    if options.force_setup:
        argv.append("--setup")
    if options.skip_setup:
        argv.append("--no-setup")
    argv += ["--log-level", options.log_level]
    return argv


def _create_requested_project(options: Options, log: logging.Logger) -> Options:
    """Handle ``--new-project NAME``; returns options with the project path set."""
    from .core.project import ProjectError, ProjectManager

    store = SettingsStore(options.config_dir)
    settings = store.load()
    manager = ProjectManager(Path(settings.sketchbook_dir).expanduser()
                             if str(settings.sketchbook_dir or "").strip() else None)
    parent = options.new_project_parent or manager.suggested_parent()
    try:
        project = manager.create_project(parent, options.new_project, board_fqbn=settings.effective_fqbn())
    except ProjectError as exc:
        print(f"Could not create the project: {exc}", file=sys.stderr)
        log.error("new-project failed: %s", exc)
        return replace(options, new_project="")
    log.info("created project %s", project.root)
    return replace(options, project=project.root, new_project="")


def _report_crash(exc: BaseException, log: logging.Logger, *, log_file: Path) -> None:
    """Best effort "the app died" message that does not need a working GUI."""
    message = (
        "Arduino Studio stopped unexpectedly.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"A full traceback was written to\n{log_file}\n\n"
        "Run 'python -m arduino_studio --check' for a quick diagnosis."
    )
    print(message, file=sys.stderr)
    try:  # a dialog is nicer when there is a desktop session to show it in
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Arduino Studio", message)
        root.destroy()
    except Exception:  # pragma: no cover - headless, or Tk is what broke
        pass


def store_log_path(options: Options) -> Path:
    """Where the crash message says the traceback went."""
    from .core.settings import SettingsStore

    try:
        return SettingsStore(options.config_dir).log_dir / LOG_FILE_NAME
    except Exception:  # pragma: no cover - only cosmetic
        return Path(LOG_FILE_NAME)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
