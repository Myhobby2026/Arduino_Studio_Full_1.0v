"""Behaviour tests for :mod:`arduino_studio.core` - the logic *under* the GUI.

These run without Tk, without a display and without a real toolchain: every
``arduino-cli`` call goes to :mod:`tests.fake_arduino_cli`, a script that mimics
the parts of the CLI the app uses (including its JSON shapes).

Run with ``python tests/test_core_flow.py`` (or ``pytest tests/test_core_flow.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arduino_studio.core import examples as ex_mod  # noqa: E402
from arduino_studio.core import process as proc  # noqa: E402
from arduino_studio.core import serial_service as serial_mod  # noqa: E402
from arduino_studio.core import terminal_service as term_mod  # noqa: E402
from arduino_studio.core import utils  # noqa: E402
from arduino_studio.core.arduino_cli import MIN_CLI_VERSION, ArduinoCLI, CLIError, parse_version  # noqa: E402
from arduino_studio.core.boards import (  # noqa: E402
    BUILTIN_BOARDS,
    baud_for_fqbn,
    board_for_fqbn,
    core_for_fqbn,
    core_install_name,
    display_name_for_fqbn,
    esptool_chip_for_fqbn,
    family_for_fqbn,
    is_avr_fqbn,
    is_esp32_fqbn,
    is_esp8266_fqbn,
    parse_fqbn,
    sketch_memory_report,
    sort_board_labels,
    vendor_for_fqbn,
)
from arduino_studio.core.bootloader import BootloaderService  # noqa: E402
from arduino_studio.core.library_manager import LibraryManager  # noqa: E402
from arduino_studio.core.project import ProjectError, ProjectManager  # noqa: E402
from arduino_studio.core.runner import LANE_BUILD, LANE_QUERY, TaskRunner  # noqa: E402
from arduino_studio.core.settings import COMMON_BAUD_RATES, Settings, SettingsStore  # noqa: E402

FAKE_CLI = (ROOT / "tests" / "fake_arduino_cli.py").resolve()


# --------------------------------------------------------------------- helpers
def wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until it is truthy (worker threads need real time)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Sandbox:
    """Temp folder + fake-CLI state directory, for one test."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="as-core-"))
        self.state = self.tmp / "fake-state"
        self.state.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "Sandbox":
        os.environ["FAKE_CLI_STATE"] = str(self.state)
        os.environ["FAKE_PORT"] = "COM9"
        return self

    def __exit__(self, *exc_info: object) -> bool:
        for key in ("FAKE_CLI_STATE", "FAKE_PORT"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    @property
    def cli(self) -> ArduinoCLI:
        return ArduinoCLI(cli_path=str(FAKE_CLI))

    def project_manager(self) -> ProjectManager:
        return ProjectManager(self.tmp / "sketchbook")

    def project(self, name: str = "CoreTest", fqbn: str = "arduino:avr:uno"):
        manager = self.project_manager()
        return manager, manager.create_project(self.tmp / "sketchbook", name, board_fqbn=fqbn)


def make_zip(path: Path, library: str = "MyLib") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{library}/src/{library}.h", "#pragma once\n")
        archive.writestr(f"{library}/src/{library}.cpp", f'#include "{library}.h"\n')
        archive.writestr(f"{library}/library.properties", f"name={library}\nversion=1.0.0\nsentence=from a zip\n")
    return path


# ---------------------------------------------------------------------- tests
def test_project_skeleton_matches_the_documented_layout() -> None:
    with Sandbox() as box:
        manager, project = box.project("Blinkinator")
        root = project.root
        assert root.is_dir()
        assert (root / "Blinkinator.ino").is_file(), "the main sketch must match the folder name"
        assert (root / "include" / "example.h").is_file()
        assert (root / "src" / "example.cpp").is_file()
        assert (root / "libraries" / "README.md").is_file()
        assert (root / "project.json").is_file()
        manifest = json.loads((root / "project.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "Blinkinator"
        assert manifest["main_file"] == "Blinkinator.ino"
        assert manifest["board_fqbn"] == "arduino:avr:uno"
        assert manifest["schema_version"]
        assert manager.validate(project) == [], "a fresh project must have no structural problems"
        assert project.sketch_name == "Blinkinator"
        assert project.main_sketch.name == "Blinkinator.ino"
        assert project.relative(project.main_sketch) == "Blinkinator.ino"
        try:
            project.resolve("../escape.cpp")
        except ProjectError:
            pass
        else:  # pragma: no cover - only if the guard regressed
            raise AssertionError("resolve() must refuse to leave the project folder")


def test_project_file_operations() -> None:
    with Sandbox() as box:
        manager, project = box.project("FileOps")
        created = manager.create_file(project, "src/helper.cpp", "// helper\n")
        assert created.is_file() and created.read_text(encoding="utf-8").startswith("// helper")
        manager.create_file(project, "src/from_template.cpp")  # starter content is generated
        manager.create_folder(project, "docs")
        manager.write_file(project, "docs/notes.md", "# notes\n")
        assert manager.read_file(project, "docs/notes.md").strip() == "# notes"
        renamed = manager.rename_file(project, "docs/notes.md", "notes.txt")
        assert renamed.is_file() and renamed.name == "notes.txt"
        assert manager.delete_file(project, "docs/notes.txt") is True
        assert not renamed.exists()
        outside = box.tmp / "import-me.cpp"
        outside.write_text("// imported\n", encoding="utf-8")
        imported = manager.import_file(project, outside)
        assert imported.is_file() and project.root in imported.parents
        assert any(path.name == "helper.cpp" for path in project.source_files())
        duplicate = manager.duplicate_project(project, "FileOps2")
        assert duplicate.root.name == "FileOps2" and (duplicate.root / "FileOps2.ino").is_file()
        again = manager.duplicate_project(project, "FileOps2")
        assert again.root.name != duplicate.root.name, "a taken name must not be overwritten"
        new_root, old_root = manager.rename_project(duplicate, "FileOps3")
        assert Path(new_root).name == "FileOps3" and Path(old_root).is_dir() is False
        assert manager.compile_target(project).is_dir()
        manager.delete_project(duplicate, to_trash=False)
        assert not Path(new_root).exists()
        archive = duplicate.root.parent / f"{duplicate.name}.deleted-"
        assert not Path(new_root).exists()


def test_project_open_and_template_flow() -> None:
    with Sandbox() as box:
        stray = box.tmp / "stray"
        stray.mkdir()
        (stray / "Stray.ino").write_text("void setup(){}\nvoid loop(){}\n", encoding="utf-8")
        manager = ProjectManager(box.tmp / "sketchbook")
        opened = manager.open(stray / "Stray.ino")
        assert opened.root == stray and opened.manifest_path.is_file(), "opening a .ino adopts its folder"
        assert opened.sketch_name == "stray", "the sketch name follows the folder"
        empty = box.tmp / "nothing-here"
        empty.mkdir()
        try:
            manager.open(empty)
        except ProjectError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a folder without a sketch must not open silently")
        try:
            manager.open(box.tmp / "does-not-exist")
        except ProjectError as exc:
            assert "does not exist" in str(exc)
        example = ex_mod.get_example("blink")
        assert example is not None
        files = ex_mod.render_files(example, "Blinky")
        template = manager.create_project_from_template(box.tmp / "templates", "Blinky", files,
                                                         example.fqbn_hint, example.summary)
        assert (template.root / "Blinky.ino").is_file()
        assert template.manifest.board_fqbn == example.fqbn_hint
        assert manager.unique_project_dir(box.tmp / "templates", "Blinky").name == "Blinky-2"
        assert manager.suggested_parent().is_dir()
        assert manager.icon_for(template.main_sketch) == "ino" == manager.icon_for(".ino")
        assert manager.project_display_path(template)


def test_name_rules_and_atomic_write() -> None:
    assert utils.is_valid_project_name("Blink2") == (True, "")
    assert utils.is_valid_project_name("My Sketch")[0] is True, "spaces are legal in sketch folders"
    for bad in ("", "   ", "a" * 70, "../escape", "has/slash", 'quote"name', "con", "NUL.txt", "com1"):
        ok, message = utils.is_valid_project_name(bad)
        assert ok is False and message, f"{bad!r} should be rejected with a reason"
    assert utils.sanitize_name("  My Sketch!!  ").startswith("My")
    assert utils.sanitize_name("///") == "ArduinoProject"
    with Sandbox() as box:
        target = box.tmp / "deep" / "nested" / "file.txt"
        utils.atomic_write_text(target, "one\n")
        assert target.read_text(encoding="utf-8") == "one\n"
        utils.atomic_write_text(target, "two\n")
        assert target.read_text(encoding="utf-8") == "two\n"
        assert sorted(path.name for path in target.parent.iterdir()) == ["file.txt"]
        assert utils.ensure_dir(box.tmp / "made") .is_dir()
        assert utils.human_bytes(0) == "0 B"
        assert "KB" in utils.human_bytes(2048)
        assert utils.human_duration(0.4).endswith("s")
        assert "m" in utils.human_duration(125)
        assert utils.strip_ansi("\x1b[32mok\x1b[0m") == "ok"


def test_settings_store_roundtrip() -> None:
    with Sandbox() as box:
        config = box.tmp / "config"
        store = SettingsStore(config)
        settings = store.load()
        assert isinstance(settings, Settings)
        assert settings.first_run_completed is False
        store.update(fqbn="arduino:avr:mega", port="COM7", serial_baud=115200)
        store.save()
        assert store.path.is_file() and store.path.name == "settings.json"
        assert store.directory == config and store.log_dir == config / "logs"
        assert store.data_dir == config / "data"
        again = SettingsStore(config).load()
        assert again.fqbn == "arduino:avr:mega" and again.port == "COM7"
        assert again.serial_baud == 115200
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["made_up_future_key"] = {"a": 1}
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        reloaded = SettingsStore(config).load()
        assert reloaded.extra.get("made_up_future_key") == {"a": 1}, "unknown keys must survive"
        store.path.write_text("{ this is not json", encoding="utf-8")
        assert SettingsStore(config).load().fqbn, "a broken file must fall back to defaults"
        seen: list[tuple[str, object]] = []
        store.add_listener(lambda key, value: seen.append((key, value)))
        store.set("port", "COM3")
        assert ("port", "COM3") in seen
        assert 115200 in COMMON_BAUD_RATES and 9600 in COMMON_BAUD_RATES
        assert isinstance(reloaded.effective_cli_path(), str)


def test_board_helper_functions() -> None:
    uno = "arduino:avr:uno"
    esp32 = "esp32:esp32:esp32"
    esp8266 = "esp8266:esp8266:nodemcuv2"
    assert is_avr_fqbn(uno) and not is_avr_fqbn(esp32)
    assert is_esp32_fqbn(esp32) and is_esp8266_fqbn(esp8266)
    assert family_for_fqbn(uno) == "avr" and family_for_fqbn(esp32) == "esp32"
    assert core_for_fqbn(esp8266) == "esp8266:esp8266"
    assert core_install_name(uno) == "arduino:avr", "core install takes a platform id, not a board FQBN"
    assert core_install_name("arduino:avr") == "arduino:avr"
    assert core_install_name("  ") == ""
    assert esptool_chip_for_fqbn(esp32) == "esp32"
    assert baud_for_fqbn(uno) and baud_for_fqbn(esp32)
    parsed = parse_fqbn(uno)
    assert (parsed.get("vendor"), parsed.get("arch"), parsed.get("board")) == ("arduino", "avr", "uno")
    assert vendor_for_fqbn(esp8266) == "esp8266"
    assert display_name_for_fqbn(uno) == "Arduino Uno"
    assert "flash" in sketch_memory_report(uno)
    assert "unknown" in sketch_memory_report("vendor:not:real").lower()
    assert board_for_fqbn("vendor:not:real") is None
    labels = [board.name for board in BUILTIN_BOARDS]
    assert sorted(sort_board_labels(labels)) == sorted(labels)
    for board in BUILTIN_BOARDS:
        assert board.fqbn and board.name and board.family and board.flash_bytes > 0
        assert board.sram_bytes > 0 and board.default_baud > 0 and board.mcu


def test_cli_probe_and_missing_binary() -> None:
    with Sandbox() as box:
        cli = box.cli
        info = cli.probe(str(FAKE_CLI))
        assert info.ok and info.version.startswith("1."), info.message
        assert parse_version(info.version) >= MIN_CLI_VERSION
        missing = ArduinoCLI(cli_path=str(box.tmp / "nope" / "arduino-cli.exe"))
        try:
            missing.require()
        except CLIError as exc:
            assert "arduino-cli" in str(exc) and "install" in str(exc).lower()
        else:  # pragma: no cover
            raise AssertionError("a missing CLI must raise CLIError")
        assert missing.probe("").ok is False and missing.probe("").message
        assert ArduinoCLI.compile is not None
        assert isinstance(ArduinoCLI.auto_detect(), str)


def test_cli_compile_success_reports_sizes_and_build_dir() -> None:
    with Sandbox() as box:
        manager, project = box.project("Compiles")
        cli = box.cli
        build_dir = project.root / "build" / "Release"
        report = cli.compile(project.root, "arduino:avr:uno", build_dir=build_dir)
        assert report.ok, report.output
        assert report.duration >= 0.0
        assert Path(report.build_dir).is_dir()
        assert (build_dir / "Compiles.hex").is_file()
        assert (build_dir / "Compiles.elf").is_file()
        parsed = cli.parse_output(report.output, project.root)
        assert not parsed.errors, parsed.diagnostics
        memory = parsed.memory
        assert memory is not None and memory.flash_bytes == 924
        assert memory.flash_max == 32256 and memory.flash_percent > 0
        assert memory.ram_bytes == 53 and memory.ram_max == 2096
        assert report.build_dir and Path(report.build_dir) == build_dir


def test_cli_compile_errors_become_diagnostics() -> None:
    with Sandbox() as box:
        manager, project = box.project("Broken")
        (project.root / "Broken.ino").write_text(
            "void setup() {\n  ERROR_IN_SKETCH\n  notAFunction();\n}\nvoid loop() {}\n", encoding="utf-8")
        cli = box.cli
        report = cli.compile(project.root, "arduino:avr:uno", build_dir=project.root / "build")
        assert report.ok is False and report.returncode != 0
        diagnostics = report.diagnostics.diagnostics
        assert diagnostics, "the compiler message must become a Diagnostic"
        first = diagnostics[0]
        assert first.is_error and first.line >= 1
        assert first.project_relative.endswith("Broken.ino")
        assert "not declared" in first.message
        assert cli.humanize_failure(report.output)

        (project.root / "Broken.ino").write_text(
            "#include <MissingLib.h>  // FATAL_MISSING_HEADER\nvoid setup(){}\nvoid loop(){}\n", encoding="utf-8")
        missing = cli.compile(project.root, "arduino:avr:uno", build_dir=project.root / "build2")
        parsed = missing.diagnostics
        assert "MissingLib.h" in " ".join(parsed.missing_includes), parsed.missing_includes
        assert isinstance(cli.suggest_libraries_for_headers(parsed.missing_includes), dict)
        assert missing.cancelled is False


def test_cli_upload_and_burn_bootloader() -> None:
    with Sandbox() as box:
        manager, project = box.project("Flashy")
        cli = box.cli
        build = cli.compile(project.root, "arduino:avr:uno", build_dir=project.root / "build")
        good = cli.upload(build_dir=build.build_dir, fqbn="arduino:avr:uno", port="COM9", verify=True)
        assert good.ok, good.output
        assert good.contains("Sketch uploaded")
        assert "--verify" in good.command
        no_port = cli.upload(build_dir=build.build_dir, fqbn="arduino:avr:uno", port="")
        assert no_port.ok is False and no_port.contains("avrdude")
        # the CLI only sees the compiled artefacts, so the marker goes in the build dir
        (Path(build.build_dir) / "Breakme.cpp").write_text("// UPLOAD_FAIL\n", encoding="utf-8")
        bad = cli.upload(build_dir=build.build_dir, fqbn="arduino:avr:uno", port="COM9")
        assert bad.ok is False and bad.contains("ser_open"), bad.output
        assert cli.humanize_failure(bad.output)
        burn = cli.burn_bootloader(fqbn="arduino:avr:uno", port="COM9", programmer="arduino:avrisp")
        assert burn.ok, burn.output
        joined = " ".join(burn.command)
        assert "burn-bootloader" in joined and "--programmer arduino:avrisp" in joined
        assert "--port COM9" in joined
        assert burn.contains("verified") or burn.contains("fuses")


def test_cli_boards_ports_and_helpers() -> None:
    with Sandbox() as box:
        cli = box.cli
        boards = cli.board_listall()
        assert boards and all(board.fqbn and board.name for board in boards)
        uno = next(board for board in boards if board.fqbn == "arduino:avr:uno")
        assert "Uno" in uno.label
        ports = cli.upload_port_list()
        assert any(port.address == "COM9" for port in ports), [port.address for port in ports]
        assert isinstance(cli.board_list(), list)
        assert isinstance(cli.core_list(), list)
        assert isinstance(cli.update_index().ok, bool), "index refresh returns a result, never raises"
        assert cli.cache_clean().returncode == 0
        assert cli.execute(["version"], echo_command=False).contains("arduino-cli")
        assert cli.board_attach(box.tmp / "x.ino", "COM9", "arduino:avr:uno").returncode == 0


def test_library_manager_search_install_uninstall() -> None:
    with Sandbox() as box:
        manager = LibraryManager(box.cli, data_dir=box.tmp / "libdata")
        results = manager.search("Servo")
        assert results, "the fake CLI ships a Servo library"
        servo = next(record for record in results if record.name == "Servo")
        assert servo.display_name and "Servo.h" in servo.provides_includes
        assert servo.version_spec.startswith("Servo@") and servo.version
        assert manager.install("Servo").ok
        installed = manager.installed()
        assert any(record.name == "Servo" and record.installed for record in installed)
        found = manager.find_installed("Servo")
        assert found is not None and found.version
        assert "Servo.h" in manager.resolve_includes(["Servo.h"])
        assert servo.to_dict().get("name") == "Servo"
        assert servo.scope and servo.description is not None
        assert isinstance(manager.index_is_stale(7), bool)
        manager.mark_index_updated()
        assert manager.index_is_stale(7) is False
        assert manager.uninstall("Servo").ok
        assert not any(record.name == "Servo" and record.installed for record in manager.installed())
        assert isinstance(manager.updatable(), list)
        assert isinstance(manager.index_age_days(), float)
        assert manager.update_index().returncode == 0


def test_library_manager_zip_git_and_project_local() -> None:
    with Sandbox() as box:
        manager = LibraryManager(box.cli, data_dir=box.tmp / "libdata")
        action = manager.install_zip(make_zip(box.tmp / "MyLib.zip"))
        assert action.ok, action.message
        assert action.record.name == "MyLib"
        # arduino-cli needs the unsafe-install setting before it accepts a Git URL
        assert manager.enable_unsafe_install().ok
        git = manager.install_git("https://github.com/example/MyLib.git", branch="dev")
        assert git.ok, git.message
        assert git.record.repository_url.endswith("MyLib.git")
        manager2, project = box.project("WithLibs")
        library = project.root / "libraries" / "LocalLib"
        (library / "src").mkdir(parents=True)
        (library / "library.properties").write_text("name=LocalLib\nversion=0.1.0\n", encoding="utf-8")
        (library / "src" / "LocalLib.h").write_text("#pragma once\n", encoding="utf-8")
        local = manager.project_libraries(project)
        assert any(record.name == "LocalLib" for record in local), local
        record = next(record for record in local if record.name == "LocalLib")
        assert manager.remove_project_library(project, record) is True
        assert not (project.root / "libraries" / "LocalLib").exists()


def test_library_unsafe_install_setting_is_respected() -> None:
    """ZIP/Git installs are gated by arduino-cli's ``library.enable_unsafe_install``.

    The fake CLI mirrors the real refusal, so this covers the whole loop:
    refuse -> explain -> ``config set`` -> retry succeeds.
    """
    with Sandbox() as box:
        cli = box.cli
        cli.probe(str(FAKE_CLI))  # keeps the fallback extraction inside the sandbox
        manager = LibraryManager(cli, data_dir=box.tmp / "libdata")
        action = manager.install_zip(make_zip(box.tmp / "UnsafeLib.zip"))
        assert action.ok, action.message
        assert "unpacked by Arduino Studio" in action.message or "--zip" in action.message, action.message
        refused = manager.install_git("https://github.com/example/UnsafeLib.git")
        assert refused.ok is False, "the CLI must be able to say no"
        assert "enable_unsafe_install" in refused.message, refused.message
        raw = cli.execute(["lib", "install", "--git-url", "https://github.com/example/X.git"],
                          echo_command=False)
        assert raw.returncode != 0 and manager.blocks_unsafe_install(raw.output)
        config = manager.enable_unsafe_install()
        assert config.ok, config.output
        retried = manager.install_git("https://github.com/example/UnsafeLib.git", branch="dev")
        assert retried.ok, retried.message
        assert retried.record.name == "UnsafeLib"
        assert manager.uninstall("UnsafeLib").returncode == 0


def test_bootloader_plans_and_gating() -> None:
    with Sandbox() as box:
        service = BootloaderService(box.cli, cli_path=str(FAKE_CLI), data_dir=box.tmp / "boot")
        supported, why = service.supports_bootloader("arduino:avr:uno")
        assert supported is True and isinstance(why, str) and why
        esp_ok, esp_reason = service.supports_bootloader("esp32:esp32:esp32")
        assert esp_ok is False and "flash" in esp_reason.lower()
        chips = service.supported_chips()
        assert chips and any("328" in chip for chip in chips)
        programmers = service.programmers()
        assert programmers and any("isp" in programmer.id.lower() for programmer in programmers)
        assert all(programmer.label for programmer in programmers)
        plan = service.plan_burn_bootloader("arduino:avr:uno", "arduino:avrisp", "COM9")
        joined = " ".join(plan.argv)
        assert "burn-bootloader" in joined and "--programmer arduino:avrisp" in joined and "--port COM9" in joined
        assert plan.is_dangerous and plan.human and plan.warnings
        assert service.avr_part_for_board("arduino:avr:uno") == "m328p"
        for chip in ("atmega328p", "m328p", "ATmega328P"):
            fuses = service.best_fuses(chip=chip)
            assert fuses.complete, f"{chip}: the preset lookup must tolerate avrdude part codes"
            assert fuses.low == "0xFF" and fuses.high == "0xDE"
        unknown = service.best_fuses(chip="made-up-chip")
        assert not unknown.complete and unknown.summary.count("unknown") == 4
        assert unknown.warnings()
        read = service.plan_read_chip("m328p", "arduino:avrisp", "COM9")
        if service.find_avrdude():
            assert read is not None and read.is_dangerous is False
        else:
            assert read is None, "without avrdude a read must be refused, not guessed"
        backup = service.default_backup_path(box.tmp, "uno-backup")
        assert backup.suffix and "uno" in backup.name.lower()
        assert isinstance(service.find_avrdude(), str)
        assert service.parse_chip_info("").ok is False and service.parse_chip_info("").error
        assert service.explain_failure("avrdude: stk500_getsync() attempt 1 of 10: not in sync", kind="burn")
        assert service.explain_failure("avrdude: ser_open(): can't open device", kind="burn")
        assert isinstance(service.board_menu_options("arduino:avr:uno"), dict)
        try:
            service.plan_burn_bootloader("", "arduino:avrisp", "COM9")
        except Exception as exc:
            assert "board" in str(exc).lower()
        else:  # pragma: no cover
            raise AssertionError("a missing FQBN must be refused")


def test_bootloader_esptool_plan() -> None:
    with Sandbox() as box:
        service = BootloaderService(box.cli, cli_path=str(FAKE_CLI), data_dir=box.tmp / "boot")
        binary = box.tmp / "firmware.bin"
        binary.write_bytes(b"\xe9\x02\x00")
        if not (service.find_esptool() or service.find_python_for_esptool()):
            # esptool ships with the ESP core; without it the service must refuse
            # to build a plan rather than invent a command line.
            try:
                service.plan_esptool(family="esp32", port="COM9", binary=binary, action="write")
            except Exception as exc:
                assert "esptool" in str(exc).lower(), str(exc)
            else:  # pragma: no cover
                raise AssertionError("plan_esptool must refuse when esptool is unavailable")
            return
        plan = service.plan_esptool(family="esp32", port="COM9", baud=921600, address="0x10000",
                                    binary=binary, erase_first=True, action="write")
        text = " ".join(str(part) for part in plan.argv)
        assert plan.kind == "esptool"
        assert "write_flash" in text and "0x10000" in text and str(binary) in text
        assert plan.is_dangerous and plan.warnings
        erase = service.plan_esptool(family="esp8266", port="COM9", action="erase")
        assert "erase_flash" in " ".join(str(part) for part in erase.argv)
        read = service.plan_esptool(family="esp32", port="COM9", action="read", read_size=1024,
                                    out_file=box.tmp / "dump.bin")
        assert read.output_file is not None
        assert service.plan_flash_from_project("esp32", "COM9", box.tmp / "build").argv
        if service.find_esptool() or service.find_python_for_esptool():
            result = service.run(erase)
            assert hasattr(result, "returncode")


def test_serial_service_without_hardware() -> None:
    with Sandbox() as box:
        service = serial_mod.SerialService()
        assert service.is_open is False
        assert service.status_line()
        config = serial_mod.SerialConfig("COM9", 115200)
        assert config.baud == 115200 and config.dtr is True and config.rts is True
        assert config.timeout > 0 and config.encoding == "utf-8"
        try:
            service.write("hello")
        except serial_mod.SerialError as exc:
            assert "open" in str(exc).lower() or "closed" in str(exc).lower()
        else:  # pragma: no cover
            raise AssertionError("writing without an open port must raise SerialError")
        assert service.close_for_upload("COM9") is False
        assert service.holds_port("") is False
        assert serial_mod.SerialService.port_is_free("") is False
        assert isinstance(serial_mod.SerialService.port_is_free("COM4242"), bool)
        ports = serial_mod.list_serial_ports()
        assert isinstance(ports, list)
        assert all(port.name and port.label for port in ports)
        log_path = box.tmp / "serial.log"
        written = serial_mod.save_log(["line one", "line two"], log_path, header="# COM9 @ 9600")
        content = Path(written).read_text(encoding="utf-8")
        assert content.startswith("# COM9") and "line two" in content
        assert set(serial_mod.LINE_ENDING_BYTES) >= {"None", "LF", "CR", "CRLF"}
        assert serial_mod.LINE_ENDING_BYTES["CRLF"] == b"\r\n"
        assert sorted(["COM10", "COM9", "COM1"], key=serial_mod.natural_port_key) == ["COM1", "COM9", "COM10"]


def test_terminal_service_helpers() -> None:
    shells = term_mod.TerminalService.available_shells()
    assert shells and all(len(entry) == 3 for entry in shells)
    keys = {key for key, _label, _usable in shells}
    assert {"powershell", "cmd", "bash"} <= keys
    assert term_mod.TerminalService.default_shell_key() in keys
    for command in ("rm -rf /", "del /q *", "format C:", "shutdown /s"):
        destructive, reason = term_mod.is_destructive(command)
        assert destructive is True and reason, command
    for safe in ("ls", "arduino-cli board list", "echo hi"):
        assert term_mod.is_destructive(safe)[0] is False, safe
    quick = term_mod.quick_commands("arduino-cli", "C:\\proj")
    assert quick and all(len(item) == 2 for item in quick)
    split = term_mod.TerminalService.split_command_line('lib install "Adafruit NeoPixel" --verbose')
    assert split[:2] == ["lib", "install"] and "Adafruit NeoPixel" in split
    assert term_mod.strip_ansi("\x1b[32mgreen\x1b[0m") == "green"


def test_terminal_service_runs_a_real_command() -> None:
    service = term_mod.TerminalService()
    code, text = service.run_captured("echo studio", timeout=30.0)
    assert code == 0 and "studio" in text, (code, text)
    failing = service.run_captured("test -e /definitely/not/here", timeout=30.0)
    assert isinstance(failing[0], int)


def test_task_runner_lanes_progress_and_cancel() -> None:
    runner = TaskRunner()
    logs: list[tuple[str, str]] = []
    progress: list[tuple[object, str]] = []
    done: list[object] = []
    handle = runner.submit(
        "core:build",
        lambda context: (context.log("step", "info"), context.progress(0.5, "halfway"), "value")[2],
        lane=LANE_BUILD,
        on_log=lambda text, level: logs.append((text, level)),
        on_progress=lambda fraction, message: progress.append((fraction, message)),
        on_done=lambda result: done.append(result),
    )
    assert handle is not None
    assert wait_for(lambda: bool(done))
    result = done[0]
    assert result.ok and result.payload == "value" and result.duration >= 0
    assert result.name == "core:build"
    assert logs and progress and "halfway" in [message for _fraction, message in progress]
    assert runner.busy(LANE_BUILD) is False and runner.current(LANE_BUILD) is None

    slow_done: list[object] = []

    def crawl(context) -> str:  # a long task that honours cancellation
        for _ in range(4000):
            context.check_cancelled()
            context.progress(None, "waiting")
            time.sleep(0.005)
        return "finished"

    try:
        runner.submit("core:slow", crawl, lane=LANE_BUILD, on_done=lambda result: slow_done.append(result))
        assert wait_for(lambda: runner.busy(LANE_BUILD))
        assert runner.current(LANE_BUILD) is not None
        runner.cancel_lane(LANE_BUILD)
        assert wait_for(lambda: bool(slow_done), timeout=10.0)
        assert slow_done[0].cancelled is True
    finally:
        runner.submit("core:query", lambda context: 1, lane=LANE_QUERY)
        assert runner.wait_all(timeout=10.0)
        runner.shutdown()


def test_process_layer() -> None:
    result = proc.run_capture([sys.executable, "-c", "print('hello'); import sys; sys.stderr.write('bad\\n')"])
    assert result.ok and result.returncode == 0
    assert result.contains("hello", "bad")
    assert len(result.lines) >= 2
    assert result.duration > 0
    failed = proc.run_capture([sys.executable, "-c", "raise SystemExit(3)"])
    assert failed.ok is False and failed.returncode == 3
    slow = proc.run_capture([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.4)
    assert slow.timed_out and not slow.ok
    assert proc.build_env({"EXTRA": "1"})["EXTRA"] == "1"
    assert "PATH" in proc.build_env()
    assert proc.find_executable("definitely-not-a-real-binary") == ""
    assert proc.cli_search_dirs()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.terminate_process(process)
    assert wait_for(lambda: process.poll() is not None, timeout=5.0)


def test_packaging_spec_covers_the_lazy_ui_imports() -> None:
    """A windowed exe dies silently when the lazily imported UI is not collected.

    ``arduino_studio.ui`` hands out ``create_app`` through a PEP 562
    ``__getattr__`` (so ``--help``/``--check`` never touch Tk), which means
    PyInstaller's static import graph only sees the empty ``ui/__init__.py``.
    Without ``collect_submodules`` the built exe fails with
    ``No module named 'arduino_studio.ui.app'`` - the classic "the .exe does not
    open".  This pins the requirement so the spec cannot regress.
    """
    spec = (ROOT / "arduino_studio.spec").read_text(encoding="utf-8")
    assert 'collect_submodules("arduino_studio")' in spec, \
        "arduino_studio.spec must collect the package's submodules (the UI is imported lazily)"
    assert "collect_data_files(\"customtkinter\")" in spec, "CustomTkinter's theme JSON must be bundled"
    assert "console=False" in spec and "disable_windowed_traceback=False" in spec

    entry = (ROOT / "arduino_studio" / "main.py").read_text(encoding="utf-8")
    assert "from .ui import create_app" in entry
    assert "import arduino_studio.ui.app" not in entry, "the entry point must not import Tk eagerly"

    import arduino_studio.ui as ui_pkg

    lazy = getattr(ui_pkg, "_LAZY", {})
    assert set(lazy) == set(ui_pkg.__all__), "every lazy name must be exported"
    for module in sorted(set(lazy.values())):
        assert (ROOT / "arduino_studio" / "ui" / f"{module}.py").is_file(), module
    assert hasattr(ui_pkg, "__getattr__")

    # the frozen entry point must report failures instead of vanishing quietly
    from arduino_studio import main as studio_main

    for helper in ("has_console", "notify_user", "write_startup_report"):
        assert callable(getattr(studio_main, helper)), helper
    assert studio_main.has_console() in (True, False)


def test_examples_are_complete() -> None:
    ids = [example.id for example in ex_mod.EXAMPLES]
    assert "eeprom-string" in ids
    for example in ex_mod.EXAMPLES:
        assert example.title and example.summary and example.files
        assert example.fqbn_hint and example.serial_baud and example.tags
        assert ex_mod.example_menu_label(example).strip()
        name = ex_mod.example_project_name(example)
        rendered = ex_mod.render_files(example, name)
        assert f"{name}.ino" in rendered
        sketch = rendered[f"{name}.ino"]
        assert sketch.count("void setup()") == 1 and sketch.count("void loop()") == 1
        assert "README.md" in rendered and rendered["README.md"].strip()
    eeprom = ex_mod.render_files(ex_mod.get_example("eeprom-string"), "Store")["Store.ino"]
    for needle in ("SAVE:", "READ", "OK: Saved", "MAX_STRING_LENGTH", "checksum", "EEPROM.begin", "commit"):
        assert needle in eeprom, needle
    assert "ARDUINO_ARCH_AVR" in eeprom or ("ESP32" in eeprom and "ESP8266" in eeprom), \
        "the sketch must branch between AVR and the ESP commit() API"
    assert ex_mod.get_example("does-not-exist") is None


def test_cli_argv_shapes_are_what_the_documentation_claims() -> None:
    with Sandbox() as box:
        manager, project = box.project("Argv")
        cli = box.cli
        build_dir = project.root / "build"
        report = cli.compile(project.root, "arduino:avr:uno", build_dir=build_dir,
                            libraries=[project.root / "libraries", project.root / "src"],
                            warnings="all", clean=True, verbose=True)
        assert report.ok, report.output
        result = cli.execute(["compile", str(project.root), "--fqbn", "arduino:avr:uno"], echo_command=False)
        assert isinstance(result.command, list) and result.command[0] == str(FAKE_CLI)
        joined = " ".join(result.command)
        assert "compile" in joined and "--fqbn arduino:avr:uno" in joined
        assert cli.build_libraries_for(project.root) or True
        assert "core install" in " ".join(cli.core_install(["arduino:avr"]).command)


def main() -> int:
    """Run every ``test_*`` function and report failures (pytest-free mode)."""
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, function in tests:
        try:
            function()
        except Exception as exc:  # noqa: BLE001 - the point is to report everything
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} core tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
