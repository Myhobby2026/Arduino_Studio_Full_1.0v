#!/usr/bin/env python3
"""Sanity-check a PyInstaller build of Arduino Studio.

    python tools/check_dist.py            # checks "dist/Arduino Studio"
    python tools/check_dist.py --verbose

Run after ``build_exe.bat`` (which calls it for you).  It answers the one
question that matters when the built ``Arduino Studio.exe`` "does not open":
*is the bundle actually complete?*  A windowed exe that is missing a module
exits instantly without printing anything, so this checks PyInstaller's own
warning file and the collected module list instead of guessing.

Exit status: 0 = looks good, 1 = the build is broken (the messages say how).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DIST_DEFAULT = "Arduino Studio"

#: modules that must be inside the bundle for the app to start
REQUIRED_MODULES = (
    "arduino_studio.main",
    "arduino_studio.ui",
    "arduino_studio.ui.app",          # pulled in lazily -> needs collect_submodules()
    "arduino_studio.ui.code_editor",
    "arduino_studio.core.arduino_cli",
    "arduino_studio.core.bootloader",
    "customtkinter",
    "tkinter",
)


def _note(lines: list[str], ok: bool, message: str) -> bool:
    lines.append(("  ok   " if ok else "  FAIL ") + message)
    return ok


def _find_toc_files(build_dir: Path) -> list[Path]:
    """Every PyInstaller table of contents / warning file it wrote."""
    found: list[Path] = []
    if not build_dir.is_dir():
        return found
    for pattern in ("*/Analysis-*.toc", "*/warn-*.txt", "*/EXE-*.toc", "*/PKG-*.toc"):
        found.extend(sorted(build_dir.glob(pattern)))
    return found


def _read(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:  # pragma: no cover - unreadable temp file
            continue
    return "\n".join(chunks)


def check(dist: Path, build_dir: Path, *, verbose: bool = False) -> tuple[bool, list[str]]:
    """Return ``(everything_ok, report_lines)`` for the given build folders."""
    lines: list[str] = []
    ok = True

    exe = dist / f"{DIST_DEFAULT}.exe"
    if not exe.exists():
        candidates = sorted(dist.glob("*.exe")) if dist.is_dir() else []
        exe = candidates[0] if candidates else exe
    ok &= _note(lines, exe.is_file(), f"executable present: {exe}")
    if not exe.is_file():
        lines.append("       -> the build never produced an exe; run build_exe.bat again and read")
        lines.append("          the PyInstaller output above the error.")

    if exe.is_file():
        size = exe.stat().st_size
        ok &= _note(lines, size >= 200_000,
                    f"executable size looks sane ({size // 1024} KB)"
                    if size >= 200_000 else f"executable suspiciously small ({size} bytes)")

    internal = dist / "_internal"
    layout_ok = internal.is_dir() or any(dist.glob("base_library.zip")) or any(dist.glob("*.dll"))
    ok &= _note(lines, layout_ok, f"one-folder payload next to the exe ({internal.name}/)")
    if not layout_ok:
        lines.append("       -> copy the WHOLE 'Arduino Studio' folder, not just the .exe.")

    if internal.is_dir():
        themes = list(internal.glob("customtkinter/**/*.json"))
        ok &= _note(lines, bool(themes), f"CustomTkinter theme files collected ({len(themes)} json)")
        if not themes:
            lines.append("       -> the spec must keep: datas += collect_data_files(\"customtkinter\")")

    tables = _find_toc_files(build_dir)
    blob = _read(tables)
    if verbose or blob:
        for module in REQUIRED_MODULES:
            hit = module in blob
            ok &= _note(lines, hit, f"bundle mentions {module}")
            if not hit and tables:
                lines.append(f"       -> add {module!r} to hiddenimports (or keep collect_submodules) in")
                lines.append("          arduino_studio.spec, then rebuild.")
            elif not hit:
                lines.append(f"       -> could not verify {module!r}: no build tables in {build_dir}")

    missing = [line for line in blob.splitlines() if "missing module named arduino_studio" in line]
    if missing:
        ok = False
        lines.append(f"  FAIL PyInstaller reported {len(missing)} missing arduino_studio module(s):")
        lines.extend(f"       {line.strip()}" for line in missing[:8])
    elif tables:
        lines.append("  ok   no missing arduino_studio modules in the build warnings")

    if not tables:
        lines.append(f"  note no build tables found in {build_dir} - module checks were skipped")
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="verify a PyInstaller build of Arduino Studio")
    parser.add_argument("--dist", default=str(ROOT / "dist" / DIST_DEFAULT),
                        help="the built folder (default: dist/Arduino Studio)")
    parser.add_argument("--build", default=str(ROOT / "build" / DIST_DEFAULT),
                        help="PyInstaller's build folder (default: build/Arduino Studio)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every check, even when passing")
    parser.add_argument("--log", default="", help="also write the report to this text file")
    args = parser.parse_args(argv)

    dist, build_dir = Path(args.dist).expanduser(), Path(args.build).expanduser()
    ok, lines = check(dist, build_dir, verbose=bool(args.verbose))
    report = "\n".join([f"Arduino Studio build check - {dist}", *lines,
                        "", "RESULT: the bundle looks complete - double-click the exe to try it."
                        if ok else "RESULT: the bundle is incomplete - the exe will not start. "
                                   "Fix what is listed above and run build_exe.bat again."])
    print(report)
    if args.log:
        target = Path(args.log).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            print(f"(could not write {target}: {exc})", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
