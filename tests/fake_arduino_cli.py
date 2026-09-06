#!/usr/bin/env python3
"""A small stand-in for ``arduino-cli`` used by the test-suite.

It implements only the sub-commands the app calls, with output shaped like the
real tool (text and ``--format json``), so the compile / upload / library /
board flows can be exercised without a toolchain or hardware.

Behaviour switches (string searched in argv + sketch sources):

``ERROR_IN_SKETCH``      compile fails with a gcc style diagnostic
``FATAL_MISSING_HEADER`` ``fatal error: MissingLib.h: No such file or directory``
``UPLOAD_FAIL``          upload fails with an avrdude-style error
``BOARDS_EMPTY``         ``board listall`` returns nothing
``SLOW``                 sleep 3 s (cancellation / progress tests)

``FAKE_CLI_STATE`` selects the folder used to remember installed libraries and
``FAKE_PORT`` the serial port that is reported as connected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

FAKE_VERSION = "1.1.1"

BOARDS = [
    {"name": "Arduino Uno", "fqbn": "arduino:avr:uno", "properties": {"id": "uno"}},
    {"name": "Arduino Nano", "fqbn": "arduino:avr:nano", "properties": {"id": "nano"}},
    {"name": "Arduino Mega 2560", "fqbn": "arduino:avr:mega", "properties": {"id": "mega"}},
    {"name": "ESP32 Dev Module", "fqbn": "esp32:esp32:esp32", "properties": {"id": "esp32"}},
    {"name": "ESP8266 NodeMCU 1.0", "fqbn": "esp8266:esp8266:nodemcuv2", "properties": {"id": "nodemcuv2"}},
]

LIBRARIES = [
    {
        "name": "Servo",
        "author": "Michael Margolis, Arduino",
        "maintainer": "Arduino <info@arduino.cc>",
        "sentence": "Allows Arduino boards to control a variety of servo motors.",
        "paragraph": "This library can control a great variety of hobby servo motors.",
        "category": "Device Control",
        "license": "LGPL-2.1",
        "website": "http://www.arduino.cc/en/Reference/Servo",
        "repository": "https://github.com/arduino/ArduinoCore-avr",
        "includes": ["Servo.h"],
        "depends": "",
    },
    {
        "name": "DHT sensor library",
        "author": "Adafruit",
        "maintainer": "Adafruit <info@adafruit.com>",
        "sentence": "Arduino library for DHT11/DHT22 sensors.",
        "paragraph": "Supports DHT11, DHT21/22, AM2301, AM2302, AM2321.",
        "category": "Sensor",
        "license": "BSD-3-Clause",
        "website": "https://github.com/adafruit/DHT-sensor-library",
        "repository": "https://github.com/adafruit/DHT-sensor-library",
        "includes": ["DHT.h"],
        "depends": "Adafruit Unified Sensor",
    },
    {
        "name": "Adafruit Unified Sensor",
        "author": "Adafruit",
        "maintainer": "Adafruit <info@adafruit.com>",
        "sentence": "Unified sensor driver API required by Adafruit sensor libraries.",
        "paragraph": "Shared driver layer.",
        "category": "Data Processing",
        "license": "BSD-3-Clause",
        "website": "",
        "repository": "https://github.com/adafruit/Adafruit_Sensor",
        "includes": ["Adafruit_Sensor.h"],
        "depends": "",
    },
]
_LATEST = {"Servo": "1.2.2", "DHT sensor library": "1.4.6", "Adafruit Unified Sensor": "1.1.14"}
_INSTALLED_DEFAULT = {"Servo": "1.2.1"}

CORES = [
    {"name": "Arduino AVR Boards", "id": "arduino:avr", "version": "1.8.6",
     "latest": {"version": "1.8.6"}, "installed": True},
    {"name": "esp32", "id": "esp32:esp32", "version": "", "latest": {"version": "2.0.17"},
     "installed": False},
]


# ----------------------------------------------------------------------- state
def _state_dir() -> Path:
    override = os.environ.get("FAKE_CLI_STATE", "")
    base = Path(override) if override else Path(os.environ.get("TMPDIR", "/tmp")) / "fake-arduino-cli-state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _installed_libraries() -> dict[str, str]:
    state = _state_dir() / "installed.json"
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            pass
    return dict(_INSTALLED_DEFAULT)


def _set_installed(mapping: dict[str, str]) -> None:
    (_state_dir() / "installed.json").write_text(json.dumps(mapping, indent=1), encoding="utf-8")


def _emit(payload: dict, lines: list[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload))
    else:
        for line in lines:
            print(line, flush=True)
    sys.stdout.flush()


def _blob(paths: list[str]) -> str:
    """argv + every sketch source, used for the behaviour switches."""
    chunks = [" ".join(sys.argv)]
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if candidate.suffix.lower() in {".ino", ".cpp", ".c", ".h", ".hpp"}:
                    chunks.append(_read(candidate))
        elif path.is_file():
            chunks.append(_read(path))
    return "\n".join(chunks)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_sketch(paths: list[str]) -> Path:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            return path
        if path.is_dir():
            inos = sorted(path.glob("*.ino"))
            if inos:
                return inos[0]
            for child in sorted(path.rglob("*.ino")):
                return child
    return Path(paths[0]) if paths else Path(".")


# ------------------------------------------------------------------- commands
def cmd_version(args: argparse.Namespace) -> int:
    _emit({"version": FAKE_VERSION, "revision": "fake", "date": "2026-01-01T00:00:00Z"},
          [f"arduino-cli  Version: {FAKE_VERSION} Commit: fake Date: 2026-01-01T00:00:00Z"],
          args.format_json)
    return 0


def cmd_board_listall(args: argparse.Namespace) -> int:
    boards = [] if "BOARDS_EMPTY" in _blob([]) else BOARDS
    lines = [f"{board['name']:<28} {board['fqbn']}" for board in boards] or ["No boards found."]
    _emit({"boards": boards}, lines, args.format_json)
    return 0


def cmd_board_list(args: argparse.Namespace) -> int:
    address = os.environ.get("FAKE_PORT", "COM9")
    payload = {"detected_ports": [{
        "port": {"address": address, "label": "Arduino Uno", "protocol": "serial",
                 "protocol_label": "Serial Port (USB)"},
        "boards": [{"name": "Arduino Uno", "fqbn": "arduino:avr:uno"}],
    }]}
    _emit(payload, [f"Port   Board          Protocol", f"{address}  Arduino Uno  serial"], args.format_json)
    return 0


def cmd_upload_port_list(args: argparse.Namespace) -> int:
    address = os.environ.get("FAKE_PORT", "COM9")
    payload = {"detected_ports": [{"port": {"address": address, "label": "USB Serial",
                                           "protocol": "serial", "protocol_label": "Serial Port (USB)"},
                                  "boards": []}]}
    _emit(payload, [address], args.format_json)
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    paths = list(args.paths or ["."])
    text = _blob(paths)
    sketch = _find_sketch(paths)
    if "SLOW" in text:
        time.sleep(float(os.environ.get("FAKE_SLOW_SECONDS", "3")))
    build_path = args.build_path or args.output_dir or str(sketch.parent / "build")
    Path(build_path).mkdir(parents=True, exist_ok=True)
    (Path(build_path) / f"{sketch.stem}.elf").write_bytes(b"\x7fELF fake binary")
    (Path(build_path) / f"{sketch.stem}.hex").write_text(":00000001FF\n", encoding="ascii")
    fqbn = args.fqbn or "arduino:avr:uno"

    if "ERROR_IN_SKETCH" in text:
        lines = [f"Compiling sketch {sketch.name} for {fqbn}",
                 f"{sketch}:3:5: error: 'notAFunction' was not declared in this scope",
                 "   3 |     notAFunction();",
                 "      |     ^~~~~~~~~~~~",
                 "compilation terminated.",
                 "Error during build: exit status 1"]
        payload = {"compiler_out": "\n".join(lines),
                   "build_path": build_path,
                   "board_platform": {"id": "arduino:avr", "version": "1.8.6"},
                   "diagnostics": [{
                       "severity": "error",
                       "message": "'notAFunction' was not declared in this scope",
                       "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 16}},
                       "source_location": {"file": str(sketch), "first_line": 3}}]}
        _emit(payload, lines, args.format_json)
        return 1

    if "FATAL_MISSING_HEADER" in text:
        lines = [f"Compiling sketch {sketch.name} for {fqbn}",
                 f"{sketch}:2:10: fatal error: MissingLib.h: No such file or directory",
                 "compilation terminated.",
                 "Error during build: exit status 1"]
        payload = {"compiler_out": "\n".join(lines), "build_path": build_path,
                   "diagnostics": [{
                       "severity": "error",
                       "message": "MissingLib.h: No such file or directory",
                       "range": {"start": {"line": 1, "character": 9}, "end": {"line": 1, "character": 21}},
                       "source_location": {"file": str(sketch), "first_line": 2}}]}
        _emit(payload, lines, args.format_json)
        return 1

    lines = [f"Compiling sketch {sketch.name} for {fqbn}",
             "Sketch uses 924 bytes (2%) of program storage space. Maximum is 32256 bytes.",
             "Global variables use 53 bytes (2%) of dynamic memory, leaving 2043 bytes for local variables. "
             "Maximum is 2096 bytes.",
             f"Build directory: {build_path}"]
    payload = {"compiler_out": "\n".join(lines), "build_path": build_path,
               "size_used": 924, "size_max": 32256, "using_build_cache": False,
               "board_platform": {"id": fqbn.split(":")[0] + ":" + fqbn.split(":")[1] if fqbn.count(":") > 1
                                  else "arduino:avr", "version": "1.8.6"},
               "diagnostics": [{"severity": "warning", "message": "unused variable 'x'",
                                "range": {"start": {"line": 3, "character": 6}, "end": {"line": 3, "character": 7}},
                                "source_location": {"file": str(sketch), "first_line": 4}}]}
    if args.export_binaries:
        payload["exported_binary_files"] = {"sketch.hex": str(Path(build_path) / f"{sketch.stem}.hex")}
    _emit(payload, lines, args.format_json)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    text = _blob([args.input_dir] if args.input_dir else [])
    if "UPLOAD_FAIL" in text or not args.port:
        lines = ["avrdude: ser_open(): can't open device: The system cannot find the file specified.",
                 "error during upload"]
        _emit({"error": "avrdude: ser_open(): can't open device", "uploader_out": "\n".join(lines)},
              lines, args.format_json)
        return 1
    lines = [f"Uploading to {args.port}",
             f"Using programmer: {args.programmer or 'arduino:avrdude'}",
             "Writing 0x0000 -> 0x03a4",
             "Verifying 0x0000 -> 0x03a4",
             "Hard resetting via RTS pin...",
             "Sketch uploaded."]
    _emit({"uploader_out": "\n".join(lines)}, lines, args.format_json)
    return 0


def cmd_burn_bootloader(args: argparse.Namespace) -> int:
    if not args.programmer:
        print("error: no programmer specified", file=sys.stderr)
        return 1
    lines = [f"Running programmer: {args.programmer}",
             "avrdude: erasing chip",
             "avrdude: 32768 bytes of flash written",
             "avrdude: 32768 bytes of flash verified",
             "avrdude: 10 bytes of fuses read",
             "avrdude: 4 bytes of lock read",
             "Bootloader burned."]
    _emit({"output": "\n".join(lines)}, lines, args.format_json)
    return 0


def _library_record(name: str, installed_version: str) -> dict:
    base = next((item for item in LIBRARIES if item["name"] == name), None)
    if base is None:
        base = {"name": name, "author": "Unknown", "maintainer": "", "sentence": "Imported library.",
                "paragraph": "", "category": "Other", "license": "", "website": "",
                "repository": "", "includes": [f"{name.replace(' ', '')}.h"], "depends": ""}
    latest = _LATEST.get(name, "1.0.0")
    record = dict(base)
    record["version"] = installed_version
    record["location_type"] = "USER"
    record["install_dir"] = str(_state_dir() / "libraries" / name) if installed_version else ""
    payload = {"library": record, "name": name, "version": installed_version,
               "latest": {"version": latest}, "installed": bool(installed_version),
               "location": "user" if installed_version else "",
               "install_dir": record["install_dir"],
               "versions": [item for item in ({"version": latest},
                                              {"version": installed_version} if installed_version else None)],
               "available": {"version": latest, "versions": [{"version": latest}]}}
    return payload


def cmd_lib(args: argparse.Namespace) -> int:
    action = args.lib_action
    installed = _installed_libraries()
    if action == "search":
        term = (args.SEARCHterms or [""])[0].lower()
        entries = [_library_record(item["name"], installed.get(item["name"], "")) for item in LIBRARIES
                   if not term or term in json.dumps(item).lower()]
        lines = [f"{entry['name']:<30} {_LATEST.get(entry['name'], '-'):<10} {entry['version'] or '-'}"
                 for entry in entries] or ["No libraries found."]
        _emit({"searched_libraries": entries, "platforms": []}, lines, args.format_json)
        return 0
    if action == "list":
        entries = [_library_record(name, version) for name, version in sorted(installed.items())]
        if args.updatable:
            entries = [entry for entry in entries
                       if entry["version"] != _LATEST.get(entry["name"], entry["version"])]
        payload = {"updatable": entries} if args.updatable else {"installed": entries, "ignore_errors": False}
        _emit(payload,
              [f"{entry['name']} {entry['version']}" for entry in entries], args.format_json)
        return 0
    if action in ("install", "uninstall", "download"):
        names = list(args.LIBRARY_NAMEs or [])
        if args.zip_path:
            archive = Path(args.zip_path)
            if not archive.is_file():
                print(f"error: zip file not found: {archive}", file=sys.stderr)
                return 1
            names.append(archive.stem)
        if args.git_url:
            url = str(args.git_url)
            if not url.startswith(("http://", "https://", "git://", "git@", "file://")):
                print(f"error: invalid --git-url {url!r}", file=sys.stderr)
                return 1
            # the CLI accepts "<url>#<branch-or-tag>"; drop the fragment for the name
            names.append(url.split("#", 1)[0].rstrip("/").split("/")[-1].removesuffix(".git"))
        for name in names:
            base, _, version = str(name).partition("@")
            if action == "uninstall":
                installed.pop(base, None)
            else:
                installed[base] = version or _LATEST.get(base, "1.0.0")
        _set_installed(installed)
        lines = [f"{action} {name}: done" for name in names] or [f"{action}: nothing to do"]
        _emit({"installed": [_library_record(name, installed.get(name, "")) for name in names],
               "success": True}, lines, args.format_json)
        return 0
    if action == "update-index":
        (_state_dir() / "lib_index.timestamp").write_text(str(time.time()), encoding="utf-8")
        _emit({"success": True}, ["Updating index: library_index.[built_in_platforms] installed"],
              args.format_json)
        return 0
    print(f"error: unsupported lib action '{action}'", file=sys.stderr)
    return 2


def cmd_core(args: argparse.Namespace) -> int:
    action = args.core_action
    if action == "list":
        entries = [core for core in CORES if core["installed"]]
        _emit({"installed": entries}, [f"{core['id']} {core['version']}" for core in entries], args.format_json)
        return 0
    if action == "search":
        _emit({"platforms": CORES}, [f"{core['id']} {core['latest']['version']}" for core in CORES],
              args.format_json)
        return 0
    if action in ("install", "uninstall", "upgrade", "update-index"):
        (_state_dir() / "core_index.timestamp").write_text(str(time.time()), encoding="utf-8")
        _emit({"success": True}, [f"{action} done"], args.format_json)
        return 0
    print(f"error: unsupported core action '{action}'", file=sys.stderr)
    return 2


def cmd_config(args: argparse.Namespace) -> int:
    payload = {
        "board_manager": {"additional_indexes": [
            "https://espressif.github.io/arduino-esp32/package_esp32_index.json"]},
        "directories": {"data": str(_state_dir() / "data"), "user": str(_state_dir() / "sketchbook")},
        "daemon": {"enable_mdns": False},
        "logging": {"level": "info"},
    }
    _emit(payload, [f"config {args.config_action}: ok"], args.format_json)
    return 0


def cmd_cache_clean(args: argparse.Namespace) -> int:
    _emit({"success": True}, ["Cache cleaned."], args.format_json)
    return 0


def cmd_noop(args: argparse.Namespace) -> int:
    _emit({"success": True}, ["ok"], getattr(args, "format_json", False))
    return 0


# ------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arduino-cli", add_help=False)
    parser.add_argument("--format", dest="format_", default="text")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config-file", default="")
    parser.add_argument("--config-file-path", default="")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--fqbn", default="")
    sub = parser.add_subparsers(dest="command")

    version = sub.add_parser("version", add_help=False)
    version.add_argument("--format", dest="format_", default="text")
    version.set_defaults(func=cmd_version)

    board = sub.add_parser("board", add_help=False)
    board_sub = board.add_subparsers(dest="board_action")
    listall = board_sub.add_parser("listall", add_help=False)
    listall.add_argument("SEARCHterms", nargs="*")
    listall.add_argument("--format", dest="format_", default="text")
    listall.set_defaults(func=cmd_board_listall)
    listing = board_sub.add_parser("list", add_help=False)
    listing.add_argument("--discover", default="")
    listing.add_argument("--format", dest="format_", default="text")
    listing.set_defaults(func=cmd_board_list)
    attach = board_sub.add_parser("attach", add_help=False)
    attach.add_argument("rest", nargs="*")
    attach.add_argument("--port", default="")
    attach.add_argument("--fqbn", default="")
    attach.set_defaults(func=cmd_noop)
    ports = board_sub.add_parser("upload-port", add_help=False)
    ports_sub = ports.add_subparsers(dest="ports_action")
    ports_list = ports_sub.add_parser("list", add_help=False)
    ports_list.add_argument("--format", dest="format_", default="text")
    ports_list.add_argument("--discover", default="")
    ports_list.set_defaults(func=cmd_upload_port_list)

    compile_ = sub.add_parser("compile", add_help=False)
    compile_.add_argument("paths", nargs="*")
    compile_.add_argument("--fqbn", default="")
    compile_.add_argument("--build-path", default="")
    compile_.add_argument("--output-dir", default="")
    compile_.add_argument("--clean", action="store_true")
    compile_.add_argument("--warnings", default="default")
    compile_.add_argument("--export-binaries", action="store_true")
    compile_.add_argument("--libraries", action="append", default=[])
    compile_.add_argument("--build-property", action="append", default=[])
    compile_.add_argument("--quiet", "-q", action="store_true")
    compile_.add_argument("--verbose", "-v", action="count", default=0)
    compile_.add_argument("--format", dest="format_", default="text")
    compile_.add_argument("--json", action="store_true")
    compile_.set_defaults(func=cmd_compile)

    upload = sub.add_parser("upload", add_help=False)
    upload.add_argument("--input-dir", default="")
    upload.add_argument("--build-dir", default="")
    upload.add_argument("--input-file", default="")
    upload.add_argument("--port", default="")
    upload.add_argument("--board", "--fqbn", dest="board", default="")
    upload.add_argument("-b", "--baud", default="")
    upload.add_argument("--verify", action="store_true")
    upload.add_argument("--no-verify", action="store_true")
    upload.add_argument("--no-reset", action="store_true")
    upload.add_argument("--allow-unverified", action="store_true")
    upload.add_argument("--programmer", default="")
    upload.add_argument("--verbose", "-v", action="count", default=0)
    upload.add_argument("--format", dest="format_", default="text")
    upload.add_argument("--json", action="store_true")
    upload.set_defaults(func=cmd_upload)

    uports = sub.add_parser("upload-port", add_help=False)
    uports_sub = uports.add_subparsers(dest="upload_ports_action")
    uports_list = uports_sub.add_parser("list", add_help=False)
    uports_list.add_argument("--fqbn", default="")
    uports_list.add_argument("--format", dest="format_", default="text")
    uports_list.add_argument("--discover", default="")
    uports_list.set_defaults(func=cmd_upload_port_list)

    burn = sub.add_parser("burn-bootloader", add_help=False)
    burn.add_argument("paths", nargs="*")
    burn.add_argument("--programmer", default="")
    burn.add_argument("--port", default="")
    burn.add_argument("--fqbn", default="")
    burn.add_argument("--verbose", "-v", action="count", default=0)
    burn.add_argument("--format", dest="format_", default="text")
    burn.set_defaults(func=cmd_burn_bootloader)

    lib = sub.add_parser("lib", add_help=False)
    lib_sub = lib.add_subparsers(dest="lib_action")
    lib_search = lib_sub.add_parser("search", add_help=False)
    lib_search.add_argument("SEARCHterms", nargs="*")
    lib_search.add_argument("--format", dest="format_", default="text")
    lib_search.set_defaults(func=cmd_lib)
    lib_list = lib_sub.add_parser("list", add_help=False)
    lib_list.add_argument("--all", action="store_true")
    lib_list.add_argument("--updatable", action="store_true")
    lib_list.add_argument("--fqbn", default="")
    lib_list.add_argument("--format", dest="format_", default="text")
    lib_list.add_argument("--json", action="store_true")
    lib_list.set_defaults(func=cmd_lib)
    for name in ("install", "uninstall", "download"):
        item = lib_sub.add_parser(name, add_help=False)
        item.add_argument("LIBRARY_NAMEs", nargs="*")
        item.add_argument("--zip-path", "--zip_path", dest="zip_path", default="")
        item.add_argument("--git-url", "--git_url", dest="git_url", default="")
        item.add_argument("--git_branch", default="")
        item.add_argument("--format", dest="format_", default="text")
        item.set_defaults(func=cmd_lib)
    lib_update = lib_sub.add_parser("update-index", add_help=False)
    lib_update.add_argument("--format", dest="format_", default="text")
    lib_update.set_defaults(func=cmd_lib)

    core = sub.add_parser("core", add_help=False)
    core_sub = core.add_subparsers(dest="core_action")
    for name in ("list", "search", "install", "uninstall", "upgrade", "update-index"):
        item = core_sub.add_parser(name, add_help=False)
        item.add_argument("cores", nargs="*")
        item.add_argument("--format", dest="format_", default="text")
        item.add_argument("--all", action="store_true")
        item.set_defaults(func=cmd_core)

    config = sub.add_parser("config", add_help=False)
    config_sub = config.add_subparsers(dest="config_action")
    for name in ("dump", "add", "set", "init", "validate"):
        item = config_sub.add_parser(name, add_help=False)
        item.add_argument("rest", nargs="*")
        item.add_argument("--format", dest="format_", default="text")
        item.set_defaults(func=cmd_config)

    cache = sub.add_parser("cache", add_help=False)
    cache_sub = cache.add_subparsers(dest="cache_action")
    clean = cache_sub.add_parser("clean", add_help=False)
    clean.add_argument("--format", dest="format_", default="text")
    clean.set_defaults(func=cmd_cache_clean)

    ping = sub.add_parser("ping", add_help=False)
    ping.set_defaults(func=cmd_noop)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False)) or str(getattr(args, "format_", "text")).lower() == "json"
    if "--format" in argv:
        index = argv.index("--format")
        if index + 1 < len(argv) and str(argv[index + 1]).lower() == "json":
            as_json = True
    args.format_json = as_json
    if not getattr(args, "command", None):
        print("Usage: arduino-cli <command> [flags]", file=sys.stderr)
        return 1
    func = getattr(args, "func", None)
    if func is None:
        print(f"error: no handler for command {args.command}", file=sys.stderr)
        return 2
    try:
        return int(func(args) or 0)
    except BrokenPipeError:  # pragma: no cover - piping into head
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
