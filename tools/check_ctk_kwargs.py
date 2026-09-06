#!/usr/bin/env python3
"""Check every ``ctk.CTk*`` keyword argument against what CustomTkinter accepts.

Why this exists: CustomTkinter widgets are *not* plain Tk widgets. A widget
rejects any option it does not know - ``CTkButton(..., padx=12)`` raises
``ValueError: ['padx'] are not supported arguments`` the moment the window is
built, which in a packaged (windowed) app looks exactly like "the exe does not
open". Tk-only options such as ``padx`` belong on ``.grid()``/``.pack()``.

The checker reads the installed CustomTkinter sources (it never imports them, so
it also works on a machine without a display or without ``tkinter``) and compares
them with the constructor calls in ``arduino_studio/ui``.

    python tools/check_ctk_kwargs.py            # report + exit code
    python tools/check_ctk_kwargs.py --verbose  # show the accepted names too

Exit status: 0 = every keyword argument is supported, 1 = at least one is not.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import pathlib
import sys
from typing import Iterable, Optional

#: options that are handled by the geometry manager, never by the widget
GEOMETRY_ONLY = {"padx", "pady", "ipadx", "ipady", "anchor", "fill", "expand", "side"}

#: names CustomTkinter gives to the sets of forwarded Tk options
TK_ATTRIBUTE_SET_NAMES = ("valid_keys", "_valid_tk_label_attributes", "_valid_tk_text_attributes",
                          "_valid_tk_button_attributes", "_valid_tk_entry_attributes")


def customtkinter_root() -> Optional[pathlib.Path]:
    """Directory of the installed ``customtkinter`` package, or None.

    Searches the import path on disk first: the test suite installs a stub
    ``customtkinter`` module in :data:`sys.modules`, and ``find_spec`` would then
    report an origin of ``None`` and make the check silently do nothing.
    """
    import site

    candidates: list[pathlib.Path] = []
    for folder in list(sys.path) + list(site.getsitepackages()) + [site.getusersitepackages()]:
        if not folder:
            continue
        try:
            candidates.append(pathlib.Path(folder).expanduser() / "customtkinter" / "__init__.py")
        except OSError:  # pragma: no cover - odd path entries (zip, empty)
            continue
    for init in candidates:
        try:
            if init.is_file():
                return init.parent
        except OSError:  # pragma: no cover - permission oddities
            continue
    try:
        spec = importlib.util.find_spec("customtkinter")
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return None
    if spec is None or not spec.origin or not pathlib.Path(spec.origin).is_file():
        return None
    return pathlib.Path(spec.origin).parent


def _strings_of(node: ast.AST) -> set[str]:
    """Every string literal inside *node* (set literals, BinOps of sets, calls)."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def accepted_options(package: pathlib.Path) -> dict[str, set[str]]:
    """Map each ``CTk*`` class name to the keyword arguments it will accept."""
    table: dict[str, set[str]] = {}
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.name.startswith(("CTk",)):
                continue
            names = table.setdefault(node.name, set())
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef) and sub.name == "__init__":
                    args = sub.args
                    names |= {p.arg for p in list(args.args) + list(args.kwonlyargs)}
                    names.discard("self")
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                                and target.value.id == "self":
                            names.add(target.attr)
                        elif isinstance(target, ast.Name) and (
                                target.id in TK_ATTRIBUTE_SET_NAMES or target.id.startswith("_valid_tk")):
                            names |= _strings_of(sub.value)
                elif isinstance(sub, ast.Compare) and len(sub.comparators) == 1:
                    # `elif attribute_name == "justify":` - configure() branches list options too
                    left = sub.left
                    if isinstance(left, ast.Name) and left.id in {"attribute_name", "key", "option"}:
                        names |= {c.value for c in sub.comparators
                                  if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    return table


def _widget_name(func: ast.expr) -> Optional[str]:
    """``ctk.CTkButton`` / ``customtkinter.CTkLabel`` -> the class name, else None."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id in {"ctk", "customtkinter"} and func.attr.startswith("CTk"):
        return func.attr
    return None


def calls_in(root: pathlib.Path) -> Iterable[tuple[pathlib.Path, int, str, list[Optional[str]]]]:
    """Yield ``(file, line, widget_name, keyword_names)`` for widget construction and
    for ``.configure(...)`` on attributes that were assigned a ``ctk.CTk*`` widget.

    ``configure`` is checked as well because CustomTkinter forwards unknown options to
    the underlying Tk widget, which then raises ``tkinter.TclError`` at run time.
    """
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):  # pragma: no cover
            continue
        # which attribute holds which widget type in this module?
        attributed: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            widget = _widget_name(node.value.func)
            if widget is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    attributed[target.attr] = widget
                elif isinstance(target, ast.Name):
                    attributed[target.id] = widget
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            widget = _widget_name(node.func)
            if widget is not None:
                yield path, node.lineno, widget, [kw.arg for kw in node.keywords]
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "configure":
                owner = func.value
                name = owner.attr if isinstance(owner, ast.Attribute) else (
                    owner.id if isinstance(owner, ast.Name) else "")
                widget = attributed.get(name or "")
                if widget is not None:
                    yield path, node.lineno, widget, [kw.arg for kw in node.keywords]


def check(root: pathlib.Path, package: pathlib.Path, *, verbose: bool = False) -> tuple[int, list[str]]:
    """Return ``(problem_count, report_lines)``."""
    table = accepted_options(package)
    problems = 0
    lines: list[str] = []
    for path, line, widget, kwargs in calls_in(root):
        accepted = table.get(widget)
        if accepted is None:
            lines.append(f"  note {path.name}:{line} ctk.{widget} is not a known CustomTkinter class")
            continue
        for kwarg in kwargs:
            if kwarg is None or kwarg in accepted:
                continue
            problems += 1
            hint = "  <- geometry-only: move it to .grid()/.pack()" if kwarg in GEOMETRY_ONLY else ""
            lines.append(f"  FAIL {path.as_posix()}:{line}  ctk.{widget}(... {kwarg}=){hint}")
    if verbose:
        for widget in sorted(table):
            sample = ", ".join(sorted(name for name in table[widget] if name)[:14])
            lines.append(f"  info {widget}: {sample} ...")
    return problems, lines


#: Tk commands whose accepted options are fixed by Tk itself rather than by a
#: widget class, with the receiver name ``tests/tk_options.py`` expects.
TK_COMMANDS = {"grid": "grid", "pack": "pack", "place": "place",
               "column": "column", "heading": "heading",
               "add_command": "add_command", "add_cascade": "add_cascade",
               "add_separator": "add_separator", "add_checkbutton": "add_checkbutton",
               "add_radiobutton": "add_radiobutton"}

#: the widget kind each command belongs to (``tests/tk_options.py`` vocabulary);
#: grid/pack/place are Tk-wide, so they need no receiver.
TK_RECEIVERS = {"column": "tree", "heading": "tree", "add_command": "menu",
                "add_cascade": "menu", "add_separator": "menu",
                "add_checkbutton": "menu", "add_radiobutton": "menu"}


def tk_option_problems(scan_dir: pathlib.Path, tests_dir: Optional[pathlib.Path] = None, *,
                       verbose: bool = False) -> tuple[int, list[str]]:
    """Report misspelled Tk options such as ``Treeview.column(min_width=...)``.

    Tk validates those names inside the interpreter, so a typo is not a Python
    error: the widget call raises ``TclError: unknown option "-min_width"``
    while the main window is being built, which in a windowed executable reads
    as "the app will not open".  ``tests/tk_options.py`` holds the option
    tables; the Tk stub used by the test suite enforces them at run time, this
    function catches them without launching any UI.
    """
    scan_dir = pathlib.Path(scan_dir).resolve()
    repo_root = scan_dir.parent
    search = [pathlib.Path(tests_dir)] if tests_dir else [repo_root / "tests",
                                                          pathlib.Path(__file__).resolve().parent.parent / "tests"]
    describe = None
    for folder in search:
        if (folder / "tk_options.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            try:
                from tk_options import describe as describe, unsupported  # type: ignore[no-redef]
            except Exception:  # pragma: no cover - defensive
                describe = None
            break
    if describe is None:
        return 1, ["  FAIL tests/tk_options.py not found - cannot check Tk option names"]

    problems = 0
    lines: list[str] = []
    for path in sorted(scan_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:  # pragma: no cover - defensive
            problems += 1
            lines.append(f"  FAIL {path}: cannot parse ({exc})")
            continue
        relative = path.relative_to(repo_root).as_posix() if path.is_relative_to(repo_root) else path.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            command = TK_COMMANDS.get(node.func.attr)
            if command is None or not node.keywords:
                continue
            receiver = TK_RECEIVERS.get(node.func.attr, "")
            names = [str(keyword.arg) for keyword in node.keywords if keyword.arg]
            if any(keyword.arg is None for keyword in node.keywords):
                continue  # **kwargs - we cannot know what it expands to
            bad = unsupported(command, names, receiver)
            if not bad:
                continue
            problems += len(bad)
            lines.append(f"  FAIL {relative}:{node.lineno}: {node.func.attr}() - "
                        f"{describe(command, bad[:1], receiver)}")
    if verbose and not problems:
        lines.append("  ok   Tk geometry/Treeview/Menu option names are spelled correctly")
    return problems, lines


#: which argument of a binding call holds the sequence ("args" = index)
BINDING_CALLS = {"bind": 0, "bind_all": 0, "event_generate": 0,
                 "bind_class": 1, "tag_bind": 1, "window_bind": 1}


def binding_problems(scan_dir: pathlib.Path, tests_dir: Optional[pathlib.Path] = None, *,
                     verbose: bool = False) -> tuple[int, list[str]]:
    """Report Tk binding patterns the interpreter would refuse.

    ``bind("<Control-keypad-plus>")`` is not a Python error - Tk parses the
    pattern itself and raises ``TclError: bad event type or keysym`` when the
    widget is built, so a typo in a binding that runs during start-up looks like
    "Arduino Studio.exe will not open".  ``tests/tk_events.py`` holds the
    grammar; the Tk stub enforces it at run time, this scans it statically,
    which also catches sequences wrapped in ``try/except TclError`` (those just
    stop working instead of failing loudly).
    """
    scan_dir = pathlib.Path(scan_dir).resolve()
    repo_root = scan_dir.parent
    search = [pathlib.Path(tests_dir)] if tests_dir else [repo_root / "tests"]
    search.append(pathlib.Path(__file__).resolve().parent.parent / "tests")
    for folder in search:
        if (folder / "tk_events.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            break
    else:  # pragma: no cover - defensive
        return 1, ["  FAIL tests/tk_events.py not found - cannot check binding sequences"]
    from tk_events import describe as describe_sequence  # type: ignore[import-not-found]

    problems = 0
    lines: list[str] = []
    for path in sorted(scan_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:  # pragma: no cover - defensive
            problems += 1
            lines.append(f"  FAIL {path}: cannot parse ({exc})")
            continue
        relative = path.relative_to(repo_root).as_posix() if path.is_relative_to(repo_root) else path.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            index = BINDING_CALLS.get(node.func.attr)
            if index is None:
                continue
            arguments = list(node.args)
            if len(arguments) <= index:
                continue  # keyword form: bind(sequence=...) is not used here
            literal = arguments[index]
            if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
                continue  # built at run time - the stub validates those instead
            message = describe_sequence(str(literal.value))
            if not message:
                continue
            problems += 1
            lines.append(f"  FAIL {relative}:{node.lineno}: {message}")
    if verbose and not problems:
        lines.append("  ok   every binding pattern is one Tk understands")
    return problems, lines


def main(argv: Optional[list[str]] = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="verify CustomTkinter widget keyword arguments")
    here = pathlib.Path(__file__).resolve().parent.parent
    parser.add_argument("--package-root", default=str(here / "arduino_studio"),
                        help="folder of application code to scan (default: arduino_studio)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the accepted options as well")
    args = parser.parse_args(argv)

    package = customtkinter_root()
    if package is None:
        print("customtkinter is not installed here - skipping that part "
              "(the Tk option scan below still runs).")
        problems, lines = 0, []
    else:
        problems, lines = check(pathlib.Path(args.package_root), package, verbose=bool(args.verbose))
    print(f"CustomTkinter option check - {package.parent.name}/{package.name}")
    for line in lines or ["  ok   every ctk.CTk* keyword argument is supported"]:
        print(line)
    if problems:
        print(f"RESULT: {problems} unsupported argument(s) - CustomTkinter raises ValueError for these "
              "when the widget is built.")
        return 1
    print("RESULT: the UI only passes options the installed CustomTkinter accepts.")

    tk_problems, tk_lines = tk_option_problems(pathlib.Path(args.package_root),
                                              verbose=bool(args.verbose))
    print("\nTk option check (grid/pack/place, Treeview.column/heading, Menu.add_*)")
    for line in tk_lines or ["  ok   every Tk option name is one the interpreter knows"]:
        print(line)
    if tk_problems:
        print(f"RESULT: {tk_problems} misspelled Tk option(s) - TclError at run time.")
        return 1
    print("RESULT: Tk option names are valid too.")

    bind_problems, bind_lines = binding_problems(pathlib.Path(args.package_root),
                                                verbose=bool(args.verbose))
    print("\nTk binding pattern check (bind / bind_all / tag_bind / event_generate)")
    for line in bind_lines or ["  ok   every pattern parses as a Tk event sequence"]:
        print(line)
    if bind_problems:
        print(f"RESULT: {bind_problems} invalid binding pattern(s) - "
              "Tk raises bad event type or keysym.")
        return 1
    print("RESULT: the bindings are valid Tk sequences as well.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
