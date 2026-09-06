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
        print("customtkinter is not installed here - nothing to check (skipping).")
        return 0
    problems, lines = check(pathlib.Path(args.package_root), package, verbose=bool(args.verbose))
    print(f"CustomTkinter option check - {package.parent.name}/{package.name}")
    for line in lines or ["  ok   every ctk.CTk* keyword argument is supported"]:
        print(line)
    if problems:
        print(f"RESULT: {problems} unsupported argument(s) - CustomTkinter raises ValueError for these "
              "when the widget is built.")
        return 1
    print("RESULT: the UI only passes options the installed CustomTkinter accepts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
