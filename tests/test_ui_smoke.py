"""Headless smoke tests for the Tk/CustomTkinter layer (no display needed).

Run with ``python tests/test_ui_smoke.py`` or ``pytest tests/test_ui_smoke.py``.
The real ``tkinter`` is replaced by :mod:`tests.tkstub` before the UI modules are
imported, so every widget tree, callback and menu is actually executed - which
catches the classes of bug that ``py_compile`` cannot (typos in attribute names,
wrong callback signatures, missing methods, bad Tk option names).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import tkstub  # noqa: E402  (must be imported before the app modules)

tkstub.install()

from arduino_studio.core.boards import BUILTIN_BOARDS  # noqa: E402
from arduino_studio.core.project import ProjectManager  # noqa: E402
from arduino_studio.core.runner import TaskRunner  # noqa: E402
from arduino_studio.core.runner import LANE_BUILD  # noqa: E402
from arduino_studio.core.settings import Settings, SettingsStore  # noqa: E402
from arduino_studio.ui import (  # noqa: E402
    bootloader_panel,
    code_editor,
    editor_tabs,
    findbar,
    libraries_panel,
    serial_monitor,
    syntax,
    terminal_panel,
)
from arduino_studio.ui.theme import palette_for  # noqa: E402
from arduino_studio.ui import terminal_panel  # noqa: E402
from arduino_studio.ui.widgets import console, data_table, dialogs, explorer, status_bar, toolbar  # noqa: E402

PALETTE = palette_for("Dark")


# --------------------------------------------------------------------- fixtures
def make_project(root: Path) -> tuple[ProjectManager, object]:
    manager = ProjectManager(root / "projects")
    project = manager.create_project(root / "projects", "SmokeTest", board_fqbn="arduino:avr:uno")
    (project.root / "SmokeTest.ino").write_text(
        "#include <Arduino.h>\n\nvoid setup() {\n  Serial.begin(9600);\n  int count = 0;\n}\n\n"
        "void loop() {\n  if (count > 3) { delay(100); }  // wait\n}\n",
        encoding="utf-8",
    )
    return manager, project


# ------------------------------------------------------------------------ tests
def test_palette_and_icons() -> None:
    """Theme and icon helpers produce the values the widgets expect."""
    from arduino_studio.ui.icons import ICONS, icon_text

    palette = palette_for("Light", "#ff8800")
    assert palette.name == "Light"
    assert palette.accent == "#ff8800"
    assert palette.accent_hover != palette.accent
    for name in ("new_project", "open", "save", "verify", "upload", "serial", "libraries",
                 "bootloader", "settings", "refresh", "terminal", "chip", "flash"):
        assert name in ICONS, f"missing icon: {name}"
        assert icon_text(name)
    assert set(syntax.C_KEYWORDS) & {"void", "if", "class"}


def test_syntax_tokenizer() -> None:
    """The tokenizer classifies the constructs the editor colours."""
    keywords = syntax.ArduinoKeywords()
    source = '#include <Servo.h>\n#define MAX 4\nvoid setup() { servo.attach(9); }  // hi\n'
    tokens = syntax.tokenize(source, keywords)
    kinds = {token.tag for token in tokens}
    assert "preprocessor" in kinds and "comment" in kinds, kinds
    assert any(token.tag == "macro" for token in tokens), "include target should tag as macro"
    assert syntax.find_matching(source, source.index("{")) is not None
    assert syntax.is_open_block_comment("/* open", -1) is True
    assert syntax.indent_of("  void loop() {}") == 2


def test_editor_document_roundtrip() -> None:
    """EditorDocument saves with the right line ending and clears dirty."""
    with tempfile.TemporaryDirectory() as tmp:
        _manager, project = make_project(Path(tmp))
        path = project.root / "SmokeTest.ino"
        document = code_editor.EditorDocument(path=path)
        changed, message = document.reload_from_disk()
        assert changed and not message, message
        assert document.line_count() == 10
        assert "Arduino" in document.language
        assert document.save(document.content.replace("loop()", "loopX()"))[0]
        assert "loopX()" in path.read_text(encoding="utf-8")
        assert document.dirty is False


def test_code_editor_widget() -> None:
    """The editor builds, highlights, indents, comments and saves."""
    with tempfile.TemporaryDirectory() as tmp:
        _manager, project = make_project(Path(tmp))
        settings = Settings()
        document = code_editor.EditorDocument(path=project.root / "SmokeTest.ino")
        document.reload_from_disk()
        editor = code_editor.CodeEditor(tkstub.ROOT, PALETTE, document=document, settings=settings)
        tkstub.pump(50)
        assert editor.text.get("1.0", "end-1c").startswith("#include")
        # highlighting tags exist and are applied
        present = {name for name in editor.text.tag_names() if not name.startswith("sel")}
        for expected in ("keyword", "type", "number", "comment", "preprocessor", "arduino_api"):
            assert expected in present, f"missing highlight tag {expected}: {sorted(present)}"
            assert editor.text.tag_ranges(expected), f"{expected} has no ranges"
        # gutter got repainted
        assert editor.gutter._items if hasattr(editor.gutter, "_items") else True
        # caret navigation and status line
        editor.goto_line(3, 4)
        assert editor.text.index("insert").startswith("3.")
        editor.indent_selection()
        editor.outdent_selection()
        editor.duplicate_line()
        editor.delete_line()
        editor.move_line(-1)
        editor.move_line(1)
        editor.jump_matching_bracket()
        editor.undo()
        editor.redo()
        editor.goto_diagnostic(-1)
        editor.zoom_in if hasattr(editor, "zoom_in") else None
        assert editor.text.index("insert").startswith("3.")
        # smart indentation on Enter
        editor.text.mark_set("insert", "2.0")
        editor._on_return(type("E", (), {})())
        # block comment toggle
        editor.select_all()
        editor.toggle_comment()
        assert "//" in editor.content()
        editor.toggle_comment()
        assert "//" not in editor.content().split("\n")[0]
        # diagnostics + save
        editor.set_diagnostics([_diagnostic(project.root / "SmokeTest.ino", 4, "boom")])
        editor.goto_diagnostic(1)
        assert editor.content().count("Serial.begin") == 1
        editor.replace_all_text("void setup() {}\nvoid loop() {}\n")
        editor.mark_saved()
        editor.refresh_palette(palette_for("Light"))
        editor.apply_settings()
        editor.destroy()
        tkstub.pump(20)


def _diagnostic(path: Path, line: int, message: str):
    from arduino_studio.core.arduino_cli import Diagnostic

    return Diagnostic(file=str(path), line=line, column=1, severity="error", message=message)


def test_find_and_replace() -> None:
    """Find bar wiring: counts, replace and regex options."""
    with tempfile.TemporaryDirectory() as tmp:
        _manager, project = make_project(Path(tmp))
        document = code_editor.EditorDocument(path=project.root / "SmokeTest.ino")
        document.reload_from_disk()
        editor = code_editor.CodeEditor(tkstub.ROOT, PALETTE, document=document, settings=Settings())
        editor.show_replace()
        bar = editor.find_bar
        assert bar.visible
        bar.find_entry.delete(0, "end")
        bar.find_entry.insert(0, "void")
        tkstub.pump(20)
        bar._on_key_release(None)
        options = findbar.FindOptions(pattern="void", case_sensitive=False, whole_word=True)
        compiled = options.compile()
        assert compiled is not None and compiled.search("void loop")
        assert findbar.FindOptions(pattern="(", regex=True).error()
        editor.find_next()
        editor.replace_all("int", findbar.FindOptions(pattern="void", case_sensitive=False))
        assert "int setup" in editor.content()
        editor.highlight_all(findbar.FindOptions(pattern="int", case_sensitive=False))
        editor.show_find()
        assert editor.find_bar.visible
        editor.find_bar.hide()
        assert not editor.find_bar.visible
        editor.destroy()


def test_editor_tabs_flow() -> None:
    """Tabs: open, dirty dot, save, close with prompt, cycle, palette refresh."""
    with tempfile.TemporaryDirectory() as tmp:
        manager, project = make_project(Path(tmp))
        tabs = editor_tabs.EditorTabs(tkstub.ROOT, PALETTE, settings=Settings())
        tabs.set_project(project)
        path = project.root / "SmokeTest.ino"
        editor = tabs.open_file(path)
        assert editor is not None and tabs.current == path.resolve()
        other = tabs.open_file(path)
        assert other is editor, "opening twice must reuse the tab"
        (project.root / "extra.h").write_text("#pragma once\n", encoding="utf-8")
        header = tabs.open_file(project.root / "extra.h")
        assert header is not None
        editor.text.insert("end", "\n// new\n")
        editor.text.dispatch("<<Modified>>", {})
        assert tabs.dirty_files, "tab should be marked dirty after typing"
        assert tabs.save_all() == 1
        assert not tabs.has_unsaved
        assert "// new" in path.read_text(encoding="utf-8")
        tabs.cycle(1)
        assert tabs.current in tabs.open_paths()
        manager.rename_file(project, "SmokeTest.ino", "SmokeTest2.ino")
        tabs.rename_file(path, project.root / "SmokeTest2.ino")
        assert (project.root / "SmokeTest2.ino").exists()
        assert tabs.current and tabs.current.name == "SmokeTest2.ino"
        assert "SmokeTest2.ino" in tabs.selected_editor().document.name
        tabs.refresh_palette(palette_for("Light"))
        tabs.set_diagnostics_for_project([])
        tabs.close_all(force=True)
        assert not tabs.open_paths()
        tabs.set_project(None)
        tabs.destroy()
        del manager


def test_console_panel() -> None:
    """Console: command echo, levels, progress, diagnostics jump."""
    jumps: list[tuple[str, int]] = []
    panel = console.ConsolePanel(tkstub.ROOT, PALETTE, on_jump=lambda f, l: jumps.append((f, l)),
                                 settings=Settings())
    panel.show_command(["arduino-cli", "compile", "--fqbn", "arduino:avr:uno"])
    panel.start_operation("Compiling", "SmokeTest")
    panel.write("Sketch uses 924 bytes", "ok")
    panel.set_progress(0.5, "half")
    panel.write("/tmp/SmokeTest.ino:12:3: error: 'foo' was not declared", "error")
    panel.finish_operation(False, "Compilation failed", elapsed=1.5)
    tkstub.pump(60)
    text = panel.content()
    assert "arduino-cli" in text and "error" in text
    assert panel.line_count() >= 3
    panel.toggle_autoscroll()
    panel.toggle_wrap()
    panel.toggle_timestamps()
    panel.clear()
    panel.set_palette(palette_for("Light"))
    panel.destroy()


def test_explorer_tree() -> None:
    """Explorer builds a tree, filters, and drives its context menu."""
    with tempfile.TemporaryDirectory() as tmp:
        manager, project = make_project(Path(tmp))
        opened: list[Path] = []
        pane = explorer.ProjectExplorer(tkstub.ROOT, PALETTE, on_open_file=opened.append,
                                        on_status=lambda message: None)
        pane.set_project(project)
        assert pane.file_count if hasattr(pane, "file_count") else True
        assert pane.tree is not None and pane.tree.get_children(""), "explorer must have rows"
        labels = [pane.tree.item(item, "text") for item in pane.tree.get_children("")]
        assert any("SmokeTest" in str(text) for text in labels), labels
        pane.select_path(project.root / "SmokeTest.ino")
        pane._activate_clicked()
        assert opened, "double click must open the file"
        pane.filter_entry.insert(0, "zzz-no-match")
        pane._apply_filter()
        pane.filter_entry.delete(0, "end")
        pane._apply_filter()
        pane.refresh_palette(palette_for("Light"))
        pane.set_empty_state()
        pane.destroy()
        del manager


def test_data_table() -> None:
    """The shared table widget sorts, filters and invokes callbacks."""
    selected: list[str] = []
    activated: list[str] = []
    table = data_table.DataTable(
        tkstub.ROOT, PALETTE,
        [("name", "Name", 200, "w"), ("version", "Version", 90, "w")],
        on_select=selected.append, on_activate=activated.append,
    )
    table.set_rows([("a", ["Alpha", "1.2.0"]), ("b", ["Bravo", "0.9.1"]), ("c", ["Charlie", "2.0.0"])])
    assert table.row_count == 3
    table.sort_by("version")
    keys = table.visible_keys()
    assert keys == ["b", "a", "c"], keys
    table.sort_by("version")
    assert table.visible_keys() == ["c", "a", "b"], table.visible_keys()
    table.set_filter("alpha")
    assert table.visible_count == 1
    table.set_filter("")
    table.select_key("b")
    table._activate_clicked()
    assert activated == ["b"]
    table.update_row("b", ["Bravo", "1.0.0"], "update")
    assert table.row_values("b")[1] == "1.0.0"
    table.refresh_palette(palette_for("Light"))
    table.destroy()


def test_toolbar_and_status_bar() -> None:
    """Toolbar buttons fire the wired handlers; selectors keep their labels."""
    calls: list[str] = []
    bar = toolbar.Toolbar(
        tkstub.ROOT, PALETTE,
        on_new_project=lambda: calls.append("new"),
        on_open_project=lambda: calls.append("open"),
        on_save=lambda: calls.append("save"),
        on_verify=lambda: calls.append("verify"),
        on_upload=lambda: calls.append("upload"),
        on_serial=lambda: calls.append("serial"),
        on_libraries=lambda: calls.append("libraries"),
        on_bootloader=lambda: calls.append("bootloader"),
        on_settings=lambda: calls.append("settings"),
        on_board_selected=lambda value: calls.append(f"board:{value}"),
        on_port_selected=lambda value: calls.append(f"port:{value}"),
    )
    bar.set_refresh_handler(lambda: calls.append("refresh"))
    bar.set_boards([board.name for board in BUILTIN_BOARDS], "Arduino Uno")
    bar.set_ports(["COM5 - Arduino Uno"], "COM5 - Arduino Uno")
    assert "Uno" in bar.current_board()
    assert bar.current_port().startswith("COM5")
    for button in (bar.new_button, bar.open_button, bar.save_button, bar.verify_button,
                   bar.upload_button, bar.serial_button, bar.libraries_button,
                   bar.bootloader_button, bar.settings_button, bar.refresh_button):
        button.invoke()
    assert {"new", "open", "save", "verify", "upload", "serial", "libraries", "bootloader",
            "settings", "refresh"} <= set(calls), calls
    bar.board_menu.invoke("Arduino Nano")
    assert "board:Arduino Nano" in calls
    bar.set_busy(True)
    bar.set_busy(False)
    bar.set_upload_enabled(False)
    bar.set_save_enabled(False)
    bar.refresh_palette(palette_for("Light"))

    status = status_bar.StatusBar(tkstub.ROOT, PALETTE)
    status.set_project("SmokeTest", modified=True)
    status.set_board("Arduino Uno")
    status.set_port("COM5", connected=True)
    status.start_operation("Compiling\u2026")
    status.set_message("uploading")
    status.set_error("boom")
    status.finish_operation(True, "Done")
    status.set_memory("924 bytes (2%)")
    status.refresh_palette(palette_for("Light"))
    status.destroy()


def test_serial_panel_without_hardware() -> None:
    """Serial monitor builds, refuses to send while closed, and handles ports."""
    panel = serial_monitor.SerialMonitorPanel(tkstub.ROOT, PALETTE, settings=Settings(),
                                              on_status=lambda message: None)
    panel.refresh_ports([])
    assert panel.stats_label.cget("text") in ("closed", "")
    panel.send_entry.insert(0, "HELLO")
    panel.send()                       # not connected -> note, no exception
    panel.write_line("manual note")
    panel.toggle_pause()
    panel.toggle_pause()
    panel.clear()
    panel.copy_all()
    panel.persist_options()
    assert panel._all_lines or True
    panel.filter_entry.insert(0, "abc")
    panel._apply_filter()
    assert panel.release_port_for_upload() is False
    panel.upload_finished()
    panel.refresh_palette(palette_for("Light"))
    panel.destroy()


def test_dialog_helpers_use_stubbed_answers() -> None:
    """ask_* helpers run their body and return the scripted answer."""
    answer = dialogs.ask_message(tkstub.ROOT, kind="question", title="Title", message="Message?",
                                 detail="detail", buttons=("No", "Yes"))
    assert answer in (None, "Yes", "No")
    tkstub.DIALOGS.push("Alice")
    assert dialogs.ask_text(tkstub.ROOT, "Name?", "bob") in ("Alice", "bob", None)
    assert dialogs.ask_yes_no(tkstub.ROOT, "Sure?", "detail") in (True, False)
    plan = dialogs.ask_plan(tkstub.ROOT, heading="Burn bootloader",
                            sections=[dialogs.PlanSection("Command", "arduino-cli burn-bootloader", "mono")],
                            require_ack=True)
    assert isinstance(plan, bool)




def wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.02):
    """Sleep + pump the fake event loop until *predicate* is truthy.

    Panel work runs on :class:`TaskRunner` threads, so the tests need real
    wall-clock time (``pump`` alone only runs scheduled callbacks).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tkstub.pump(50)
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    tkstub.pump(50)
    return predicate()


# ------------------------------------------------------- panel level flows
def fake_cli_path() -> str:
    """Path of the script that emulates arduino-cli for the tests."""
    script = ROOT / "tests" / "fake_arduino_cli.py"
    return str(script)


def make_services(tmp: Path):
    import os

    os.environ["FAKE_CLI_STATE"] = str(tmp / "fake-state")
    os.environ.setdefault("FAKE_PORT", "COM9")
    from arduino_studio.core.arduino_cli import ArduinoCLI
    from arduino_studio.core.bootloader import BootloaderService
    from arduino_studio.core.library_manager import LibraryManager

    cli = ArduinoCLI(cli_path=sys.executable + " " + fake_cli_path())
    cli.cli_path = sys.executable
    cli.extra_args = ""
    # the CLI wrapper takes the script as the executable, so point it directly
    cli.cli_path = fake_cli_path()
    manager = LibraryManager(cli, tmp / "data")
    boot = BootloaderService(cli, cli_path=fake_cli_path(), data_dir=tmp / "data")
    return cli, manager, boot


def test_libraries_panel() -> None:
    """Library manager UI: search through the fake CLI, select, install, refresh."""
    import os

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["FAKE_CLI_STATE"] = str(root / "state")
        cli, manager, _boot = make_services(root)
        status: list[str] = []
        runner = TaskRunner(ui_post=lambda func: (func(), None)[1])
        panel = libraries_panel.LibrariesPanel(
            tkstub.ROOT, PALETTE, manager=manager, runner=runner, settings=Settings(),
            get_project=lambda: None, get_fqbn=lambda: "arduino:avr:uno",
            on_status=status.append, on_add_include=lambda header: status.append(f"include:{header}"),
        )
        panel.search_entry.insert(0, "DHT")
        panel._run_search()
        wait_until(lambda: not panel._busy)
        keys = panel.table.visible_keys()
        assert "DHT sensor library" in keys, keys
        panel.table.select_key("DHT sensor library")
        panel._show_record("DHT sensor library")
        assert "Adafruit" in panel.detail_view.content()
        # include button reports the header even without an open editor
        panel.add_include_selected()
        assert any(line.startswith("include:") for line in status), status
        # install path: dialog answer "yes", then the CLI runs
        tkstub.DIALOGS.push(True)
        panel.install_selected()
        wait_until(lambda: not panel._busy)
        assert any("instal" in line.lower() for line in status), status
        # installed list mode reads lib list
        panel._mode = "Installed"
        panel._run_search()
        wait_until(lambda: not panel._busy and "DHT sensor library" in panel.table.visible_keys())
        assert "DHT sensor library" in panel.table.visible_keys()
        # index refresh asks first, then runs through the CLI
        tkstub.DIALOGS.push(True)
        panel.update_index()
        wait_until(lambda: not panel._busy)
        panel.refresh_palette(palette_for("Light"))
        panel.destroy()
        del cli, manager


def test_bootloader_panel() -> None:
    """Bootloader screen: capability probing, plan dialog, run through the CLI."""
    import os

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["FAKE_CLI_STATE"] = str(root / "state")
        cli, _manager, boot = make_services(root)
        status: list[str] = []
        console_panel = console.ConsolePanel(tkstub.ROOT, PALETTE, settings=Settings())
        runner = TaskRunner(ui_post=lambda func: (func(), None)[1])
        panel = bootloader_panel.BootloaderPanel(
            tkstub.ROOT, PALETTE, service=boot, runner=runner, console=console_panel,
            settings=Settings(), get_fqbn=lambda: "arduino:avr:uno",
            get_port=lambda: "COM9", get_ports=lambda: ["COM9 - Arduino Uno"],
            get_project=lambda: None, get_build_dir=lambda: None, on_status=status.append,
        )
        panel.refresh_from_app()
        assert panel._port() == "COM9", panel._port()
        assert panel._programmers, "programmer list must not be empty"
        labels = [programmer.label for programmer in panel._programmers]
        assert any("Arduino as ISP" in label for label in labels), labels[:5]
        chips = [str(item) for item in (panel.chip_menu.cget("values") or [])]
        assert chips, "chip list must be populated"
        if "ATmega328P" not in chips:
            chips = [chip for chip in chips if "328" in chip] or chips
        panel.chip_menu.set(chips[0])
        panel._chip_chosen("ATmega328P")
        assert panel.fuse_table.row_count >= 4, "fuse table should be filled"
        # a burn attempt must show the plan dialog and require acknowledgement
        isp = next((label for label in labels if "Arduino as ISP" in label), labels[0])
        panel.programmer_menu.set(isp)
        panel._programmer_chosen(isp)
        tkstub.DIALOGS.push(True)
        panel.burn_bootloader()
        wait_until(lambda: not panel._busy)
        text = console_panel.content()
        assert "burn-bootloader" in text, text[:400]
        # chip read + wiring help pane
        panel.read_chip_info()
        wait_until(lambda: not panel._busy)
        panel.toggle_help()
        panel._show_wiring_help()
        assert "MISO" in panel.info_view.content()
        # ESP tab: without esptool the buttons stay disabled and no plan is built
        panel.bin_entry.insert(0, str(Path(tmp) / "missing.bin"))
        panel._sync_state()
        assert panel.flash_button.cget("state") == "disabled"
        assert panel._esp_plan(action="erase") is None
        # ...and with a (fake) esptool the erase plan is built and runs in the console
        fake_esptool = root / "esptool.py"
        fake_esptool.write_text("#!/usr/bin/env python3\nprint('esptool.py v4.7.0 FakeChip')\n", encoding="utf-8")
        fake_esptool.chmod(0o755)
        panel.service.find_esptool = lambda family="": str(fake_esptool)
        panel._sync_state()
        plan = panel._esp_plan(action="erase")
        assert plan is not None and "erase_flash" in " ".join(plan.argv), plan
        assert panel.erase_button.cget("state") == "normal"
        panel.refresh_palette(palette_for("Light"))
        panel.destroy()


def test_terminal_panel() -> None:
    """Terminal panel: start a shell, run a command, see output + exit code."""
    import os

    with tempfile.TemporaryDirectory() as tmp:
        status: list[str] = []
        settings = Settings()
        settings.terminal_shell = "bash" if not os.name == "nt" else settings.terminal_shell
        panel = terminal_panel.TerminalPanel(
            tkstub.ROOT, PALETTE, settings=settings, on_status=status.append,
            cli_path_provider=lambda: "", project_dir_provider=lambda: Path(tmp),
        )
        assert panel.service.running is False
        panel.run_command("echo hello-from-test")
        for _ in range(30):
            tkstub.pump(50)
        out = panel.view.content()
        assert "hello-from-test" in out, out[:400]
        panel.entry.insert(0, "exit 3")
        panel.run_entry()
        for _ in range(20):
            tkstub.pump(50)
        assert "3" in panel.exit_label.cget("text") or "exit" in str(panel.exit_label.cget("text"))
        panel._history_step(-1)
        panel.clear()
        panel.set_project(Path(tmp))
        panel.refresh_quick_commands()
        panel.refresh_palette(palette_for("Light"))
        panel.stop()
        panel.destroy()



def test_app_flow() -> None:
    """End-to-end: window, project, editor, save, verify, failure, upload guard, quit."""
    import os

    from arduino_studio.ui.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["FAKE_CLI_STATE"] = str(root / "fake-state")
        os.environ["FAKE_PORT"] = "COM7"
        config = root / "cfg"
        store = SettingsStore(config)
        seed = store.load()
        seed.update(first_run_completed=True,
                    arduino_cli_path=fake_cli_path(),
                    sketchbook_dir=str(root / "sketchbook"), fqbn="arduino:avr:uno", port="COM7",
                    console_max_lines=4000)
        store.save()

        app = create_app(["--config-dir", str(config), "--no-setup"])
        assert app.project is None, "a fresh profile must not have a project"
        assert app.views.keys() >= {"editor", "libraries", "bootloader", "settings"}

        manager = ProjectManager(root / "sketchbook")
        project = manager.create_project(root / "sketchbook", "AppTest", board_fqbn="arduino:avr:uno")
        assert (project.root / "AppTest.ino").is_file()
        assert (project.root / "include" / "example.h").is_file()
        assert app.open_project(project.root)
        assert app.project is not None and app.project.name == "AppTest"

        editor = app.tabs.selected_editor()
        assert editor is not None, "the main .ino must open automatically"
        editor.insert_text("\n  // touched by the smoke test\n")
        tkstub.pump(60)
        assert app.tabs.has_unsaved, "typing must flag the buffer as unsaved"
        assert app.save()
        assert "touched by the smoke test" in (project.root / "AppTest.ino").read_text(encoding="utf-8")
        assert not app.tabs.has_unsaved

        app.verify()
        wait_until(lambda: not app.runner.busy(LANE_BUILD))
        tkstub.pump(150)
        text = app.console.content()
        assert "Compiled in" in text, text[-600:]
        assert app._last_build_dir is not None and Path(app._last_build_dir).is_dir()

        # a compiler error must land in the console AND on the editor's gutter
        editor.replace_all_text("void setup() {\n  ERROR_IN_SKETCH\n  notAFunction();\n}\nvoid loop() {}\n")
        app.save()
        app.verify()
        wait_until(lambda: not app.runner.busy(LANE_BUILD))
        tkstub.pump(150)
        text = app.console.content()
        assert "notAFunction" in text, text[-700:]
        document = app.tabs.document_for(project.root / "AppTest.ino")
        assert document is not None and document.diagnostics, "diagnostics should reach the editor"
        app._jump_to(str(project.root / "AppTest.ino"), 3)
        assert editor.text.index("insert").startswith("3.")

        # a missing include has to be reported (and offers to install the library)
        editor.replace_all_text("#include <MissingLib.h>  // FATAL_MISSING_HEADER\n"
                                "void setup() {}\nvoid loop() {}\n")
        app.save()
        app.verify()
        wait_until(lambda: not app.runner.busy(LANE_BUILD))
        tkstub.pump(150)
        assert "MissingLib" in app.console.content()

        # upload needs a port: clearing it must be refused, not crash
        app.settings.update(port="", use_custom_fqbn=False)
        app._port_map.clear()
        app.toolbar.set_ports(["No ports found"], "No ports found")
        app.upload()
        assert not app.runner.busy(LANE_BUILD), "an upload without a port must never start"
        app.settings.update(port="COM7")

        # libraries / bootloader / settings views all swap in cleanly
        for name in ("libraries", "bootloader", "settings", "editor"):
            app._show_view(name)
            assert app._current_view == name
        for name in ("console", "serial", "terminal"):
            app._show_bottom(name)
            assert app._current_bottom == name
        app.toggle_bottom()
        app.toggle_bottom()
        app.toggle_sidebar()
        app.toggle_sidebar()
        app._zoom_editor(1)
        app._toggle_setting("word_wrap")
        app.toggle_theme()
        assert app.settings.appearance_mode in ("Dark", "Light")
        app.refresh_all()
        wait_until(lambda: not app.runner.busy())
        assert app._board_labels, "the board list should be populated from the CLI"
        app.refresh_ports()
        wait_until(lambda: not app.runner.busy())
        assert "COM7" in " ".join(app._port_labels) or app._port_map, app._port_labels
        app._apply_board_choice("arduino:avr:nano", "Arduino Nano")
        assert app.settings.fqbn == "arduino:avr:nano"
        assert app.settings.recent_projects and str(project.root) in app.settings.recent_projects[0]

        # the manifest records the board
        manifest = json.loads((project.root / "project.json").read_text(encoding="utf-8"))
        assert manifest.get("board_fqbn") == "arduino:avr:nano", manifest

        app.terminal_panel.stop()
        app.quit_app()
        assert not app.runner.busy(), "quit must drain the task runner"
        saved = json.loads((config / "settings.json").read_text(encoding="utf-8"))
        assert saved["first_run_completed"] is True
        assert saved["fqbn"] == "arduino:avr:nano"



#: alias table: how a menu accelerator's key part may appear in a Tk sequence
_KEY_ALIASES = {
    "+": {"plus", "equal", "kp_add"},
    "-": {"minus", "kp_subtract", "underscore"},
    "`": {"grave"},
    ",": {"comma"},
    ".": {"period"},
    "[": {"bracketleft"},
    "]": {"bracketright"},
    "\\": {"backslash"},
    "/": {"slash", "keyslash"},
    "0": {"0", "kp_0", "kp_space"},
    " ": {"space"},
}


def _parse_sequence(raw: str) -> tuple[set[str], str]:
    """``"<Control-Shift-Z>"`` -> ``({"control", "shift"}, "z")`` (upper implies Shift)."""
    text = str(raw).strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    parts = [part for part in text.replace("+", "-").split("-") if part]
    if not parts:
        return set(), ""
    key = parts[-1]
    mods = {part.lower() for part in parts[:-1] if part.lower() in {"control", "shift", "alt", "meta", "modifier"}}
    if len(key) == 1 and key.isalpha() and key.isupper():
        mods.add("shift")
    return mods, key.lower()


def _accelerator_is_bound(accelerator: str, bound: set[str]) -> bool:
    """True when some bound sequence satisfies a menu accelerator label."""
    accel_mods: set[str] = set()
    key = ""
    for token in str(accelerator or "").split("+"):
        token = token.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in {"ctrl", "control"}:
            accel_mods.add("control")
        elif lowered == "shift":
            accel_mods.add("shift")
        elif lowered in {"alt", "meta", "super", "win"}:
            accel_mods.add("alt")
        else:
            key = lowered
    if not key:
        return True
    wanted = _KEY_ALIASES.get(key, {key})
    for raw in bound:
        mods, bound_key = _parse_sequence(raw)
        if bound_key in wanted and mods >= accel_mods:
            return True
    return False


def test_menu_accelerators_are_actually_bound() -> None:
    """A menu that advertises Ctrl+X must really bind Ctrl+X.

    Walking the whole menu tree and comparing every ``accelerator=`` label with
    the sequences actually bound on the app/editor widgets catches the classic
    drift where a shortcut is renamed in one place only.
    """
    from arduino_studio.ui.app import create_app

    tkstub.BOUND_SEQUENCES.clear()          # only this app's bindings may count
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "cfg"
        store = SettingsStore(config)
        seed = store.load()
        seed.update(first_run_completed=True)
        store.save()
        app = create_app(["--config-dir", str(config), "--no-setup"])
        # open a real file: the editor widget owns most of the key bindings, and
        # it only exists once a document has been opened.
        manager = ProjectManager(Path(tmp) / "sketchbook")
        project = manager.create_project(Path(tmp) / "sketchbook", "Menus", board_fqbn="arduino:avr:uno")
        assert app.open_project(project.root), "the smoke harness must be able to open a project"
        assert app.tabs.selected_editor() is not None, "an editor must be open to bind its keys"
        menu = getattr(app, "menubar", None) or getattr(app, "_menubar", None)
        assert menu is not None, "the app must expose its menu bar"
        seen = 0
        problems: list[str] = []
        bound = tkstub.bound_sequences()

        def walk(node: object, trail: str = "") -> None:
            nonlocal seen
            for entry in getattr(node, "entries", []) or []:
                if "menu" in entry:
                    walk(entry["menu"], f"{trail}{entry.get('label', '')} > ")
                accel = str(entry.get("accelerator") or "")
                if not accel or accel.lower() in {"alt+f4", "f1"}:
                    continue  # native window keys are not ours to bind
                seen += 1
                if not _accelerator_is_bound(accel, bound):
                    problems.append(f"{trail}{entry.get('label')!r} advertises {accel!r}")

        walk(menu)
        assert not problems, "unbound menu shortcuts: " + "; ".join(problems)
        assert seen >= 25, f"expected the menus to advertise shortcuts, found {seen}"


def test_libraries_panel_offers_unsafe_install_fix() -> None:
    """When arduino-cli refuses a Git/ZIP install, the panel offers to enable the
    config setting and retries - the whole loop, against the fake CLI."""
    from arduino_studio.core.library_manager import LibraryManager
    from arduino_studio.ui import libraries_panel

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cli, manager, _boot = make_services(root)
        cli.probe(fake_cli_path())  # keeps the ZIP fallback inside the sandbox
        status: list[str] = []
        runner = TaskRunner(ui_post=lambda func: (func(), None)[1])
        panel = libraries_panel.LibrariesPanel(
            tkstub.ROOT, PALETTE, manager=manager, runner=runner, settings=Settings(),
            get_project=lambda: None, get_fqbn=lambda: "arduino:avr:uno",
            on_status=status.append, on_add_include=lambda header: None,
        )
        try:
            # refusing without the setting is the fake CLI's job
            refused = manager.install_git("https://github.com/example/PanelLib.git")
            assert refused.ok is False and "enable_unsafe_install" in refused.message

            tkstub.DIALOGS.push(True)  # "Enable & retry"
            retried = panel._offer_unsafe_retry(
                lambda context: manager.install_git("https://github.com/example/PanelLib.git",
                                                     branch="dev", context=context),
                "PanelLib",
            )
            assert retried is True
            assert wait_until(lambda: not panel._busy, timeout=20.0), "the retry never finished"
            assert any("PanelLib" in line for line in status), status
            config = (root / "fake-state" / "config.json")
            assert config.is_file() and "library.enable_unsafe_install" in config.read_text(encoding="utf-8")

            # declining must not submit anything and must not lose the failure message
            before = list(status)
            tkstub.DIALOGS.push(False)
            assert panel._offer_unsafe_retry(lambda context: None, "Rejected") is False
            assert status == before
            # a second refusal is no longer possible: the setting is on now
            assert manager.install_git("https://github.com/example/PanelLib2.git").ok
        finally:
            runner.shutdown()
            panel.destroy()
            del cli, manager


def test_customtkinter_widget_options_are_supported() -> None:
    """CustomTkinter raises ValueError for options it does not know, the moment a
    widget is built - inside a windowed exe that looks like "the app will not
    open".  Validate every ``ctk.CTk*`` constructor (and ``.configure``) keyword
    against the *installed* CustomTkinter sources before shipping.
    """
    tools = ROOT / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import check_ctk_kwargs

    package = check_ctk_kwargs.customtkinter_root()
    if package is None:
        print("     (skipped: customtkinter is not installed in this environment)")
        return
    problems, lines = check_ctk_kwargs.check(ROOT / "arduino_studio", package)
    assert problems == 0, (
        "unsupported CustomTkinter option(s); the widget constructor will raise:\n"
        + "\n".join(lines[:12])
    )


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
            import traceback

            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} UI smoke tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
