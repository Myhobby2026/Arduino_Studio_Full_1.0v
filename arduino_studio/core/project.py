"""Project management: skeleton, ``project.json`` metadata and file operations.

A project is a *valid Arduino sketch folder*: the main ``.ino`` file has the
same name as its directory, which is exactly what ``arduino-cli compile``
requires.  On top of that Arduino Studio adds ``include/``, ``src/``,
``libraries/`` and a ``project.json`` manifest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .utils import (
    SOURCE_SUFFIXES,
    atomic_write_text,
    ensure_dir,
    get_logger,
    iso_now,
    natural_key,
    sanitize_name,
)

__all__ = [
    "ProjectError",
    "ProjectFile",
    "LibraryEntry",
    "ProjectManifest",
    "Project",
    "ProjectManager",
    "MANIFEST_NAME",
    "PROJECT_DIRS",
    "is_arduino_folder",
]

MANIFEST_NAME = "project.json"
PROJECT_DIRS: tuple[str, ...] = ("include", "src", "libraries")
SKETCH_SUFFIXES: tuple[str, ...] = (".ino",)
RESERVED_NAMES: frozenset[str] = frozenset({".git", "build", ".pio", "__pycache__", ".vscode"})


class ProjectError(RuntimeError):
    """Raised for any project operation that cannot be completed."""


@dataclass
class ProjectFile:
    """One file inside the project (used to build the explorer tree)."""

    name: str
    path: Path
    is_dir: bool = False
    children: list["ProjectFile"] = field(default_factory=list)
    size: int = 0

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def is_editable(self) -> bool:
        return not self.is_dir and self.suffix in SOURCE_SUFFIXES


@dataclass
class LibraryEntry:
    """A library referenced by a project (registry, zip, git or local folder)."""

    name: str
    version: str = ""
    source: str = "registry"          # registry | zip | git | local
    location: str = ""                # path relative to the project when local
    url: str = ""                     # git url when source == "git"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LibraryEntry":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ProjectManifest:
    """Content of ``project.json``."""

    name: str
    main_file: str = ""
    board_fqbn: str = "arduino:avr:uno"
    port: str = ""
    description: str = ""
    author: str = ""
    created: str = field(default_factory=iso_now)
    modified: str = field(default_factory=iso_now)
    schema_version: int = 1
    build_properties: dict[str, str] = field(default_factory=dict)
    extra_flags: list[str] = field(default_factory=list)
    libraries: list[LibraryEntry] = field(default_factory=list)
    open_files: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["libraries"] = [lib.to_dict() for lib in self.libraries]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectManifest":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = dict(data or {})
        libs = payload.pop("libraries", []) or []
        build_props = payload.pop("build_properties", {}) or {}
        flags = payload.pop("extra_flags", []) or []
        clean = {k: v for k, v in payload.items() if k in known}
        manifest = cls(**clean)  # type: ignore[arg-type]
        manifest.build_properties = {str(k): str(v) for k, v in dict(build_props).items()}
        manifest.extra_flags = [str(f) for f in flags if str(f).strip()]
        manifest.libraries = [
            LibraryEntry.from_dict(item) if isinstance(item, dict) else LibraryEntry(name=str(item))
            for item in libs
        ]
        return manifest


class Project:
    """An opened project: folder + manifest + file helpers."""

    def __init__(self, root: os.PathLike[str] | str, manifest: Optional[ProjectManifest] = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.manifest = manifest or ProjectManifest(name=self.root.name)
        self._log = get_logger("project")

    # ------------------------------------------------------------------ props
    @property
    def name(self) -> str:
        return self.manifest.name or self.root.name

    @property
    def main_sketch(self) -> Path:
        """Absolute path of the primary ``.ino`` file."""
        if self.manifest.main_file:
            candidate = self.root / self.manifest.main_file
            if candidate.is_file():
                return candidate
        return self.root / f"{self.root.name}.ino"

    @property
    def libraries_dir(self) -> Path:
        return ensure_dir(self.root / "libraries")

    @property
    def src_dir(self) -> Path:
        return self.root / "src"

    @property
    def include_dir(self) -> Path:
        return self.root / "include"

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def sketch_name(self) -> str:
        """Folder name = sketch name (this is what arduino-cli compiles)."""
        return self.root.name

    def build_dir(self, configuration: str = "Release") -> Path:
        """Per-configuration build output folder (kept out of the file tree)."""
        safe = re.sub(r"[^\w.\-]", "_", configuration or "Release")
        return ensure_dir(self.root / "build" / safe)

    # ------------------------------------------------------------- file lists
    def sketch_files(self) -> list[Path]:
        """All ``.ino`` files in the project root, main sketch first."""
        files = sorted(self.root.glob("*.ino"), key=lambda p: natural_key(p.name))
        main = self.main_sketch
        if main in files:
            files.remove(main)
            files.insert(0, main)
        return files

    def source_files(self) -> list[Path]:
        """All editable source files below the project root."""
        return sorted(
            (p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES),
            key=lambda p: (len(p.parts), natural_key(p.name)),
        )

    def tree(self) -> list[ProjectFile]:
        """Build the file tree shown in the explorer pane."""
        return [self._build_node(item) for item in self._sorted_children(self.root)]

    def relative(self, path: os.PathLike[str] | str) -> str:
        """Project-relative path with ``/`` separators."""
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return Path(str(path)).name

    def resolve(self, relative: os.PathLike[str] | str) -> Path:
        """Resolve *relative* inside the project, refusing escapes."""
        candidate = (self.root / str(relative)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ProjectError(f"Path escapes the project folder: {relative}")
        return candidate

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).exists()
        except ProjectError:
            return False

    def can_edit(self, path: os.PathLike[str] | str) -> bool:
        """True when *path* is a file we can open in the editor."""
        p = Path(str(path))
        return p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES

    # ------------------------------------------------------------------- save
    def save_manifest(self) -> Path:
        """Write ``project.json`` (never raises for a missing dir)."""
        self.manifest.modified = iso_now()
        if not self.manifest.main_file:
            self.manifest.main_file = self.relative(self.main_sketch)
        atomic_write_text(self.manifest_path, json.dumps(self.manifest.to_dict(), indent=2) + "\n")
        return self.manifest_path

    def add_library(self, entry: LibraryEntry) -> bool:
        """Register a library in the manifest; ``False`` if it was already there."""
        existing = {lib.name.lower(): lib for lib in self.manifest.libraries}
        key = entry.name.lower()
        if key in existing:
            before = asdict(existing[key])
            if before == asdict(entry):
                return False
            self.manifest.libraries = [
                entry if lib.name.lower() == key else lib for lib in self.manifest.libraries
            ]
            self.save_manifest()
            return True
        self.manifest.libraries.append(entry)
        self.save_manifest()
        return True

    def remove_library(self, name: str) -> bool:
        before = len(self.manifest.libraries)
        self.manifest.libraries = [lib for lib in self.manifest.libraries if lib.name.lower() != name.lower()]
        if len(self.manifest.libraries) != before:
            self.save_manifest()
            return True
        return False

    # --------------------------------------------------------------- internals
    def _sorted_children(self, folder: Path) -> list[Path]:
        try:
            items = [p for p in folder.iterdir() if p.name not in RESERVED_NAMES]
        except OSError as exc:
            self._log.debug("cannot list %s: %s", folder, exc)
            return []
        return sorted(items, key=lambda p: (not p.is_dir(), natural_key(p.name.lower())))

    def _build_node(self, path: Path) -> ProjectFile:
        node = ProjectFile(name=path.name, path=path, is_dir=path.is_dir())
        try:
            node.size = path.stat().st_size if node.is_dir is False else 0
        except OSError:
            node.size = 0
        if node.is_dir:
            node.children = [self._build_node(child) for child in self._sorted_children(path)]
        return node


def is_arduino_folder(path: os.PathLike[str] | str) -> bool:
    """A folder is a sketch when it directly contains at least one ``.ino``."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        return False
    try:
        return any(folder.glob("*.ino"))
    except OSError:
        return False


DEFAULT_SKETCH_TEMPLATE = """/*
 * {name}
 * {description}
 *
 * Created with Arduino Studio on {date}.
 * Board: {board}
 */

void setup() {{
  Serial.begin(9600);
  while (!Serial) {{
    ;  // wait for the serial port on boards with native USB
  }}
  Serial.println(F("{name}: ready"));
}}

void loop() {{
  Serial.println(F("hello from {name}"));
  delay(1000);
}}
"""

EXAMPLE_H_TEMPLATE = """#ifndef {guard}
#define {guard}

/**
 * \file {header}
 * \brief Project specific declarations.
 *
 * Files in ``include/`` are on the compiler include path, so you can write
 * ``#include "{header}"`` from any ``.ino`` or ``src`` file.
 */

#include <Arduino.h>

namespace {ns} {{

/// Number of milliseconds between two blink cycles.
constexpr unsigned long kBlinkIntervalMs = 500;

/// \return ``true`` when the elapsed time exceeds the blink interval.
bool intervalElapsed(unsigned long now, unsigned long previous);

}}  // namespace {ns}

#endif  // {guard}
"""

EXAMPLE_CPP_TEMPLATE = """/*
 * \file {source}
 * \brief Implementation of the helpers declared in ``include/{header}``.
 *
 * Everything inside ``src/`` is compiled by arduino-cli automatically, so new
 * .cpp files are picked up without touching the project settings.
 */

#include "{header}"

namespace {ns} {{

bool intervalElapsed(unsigned long now, unsigned long previous) {{
  return (now - previous) >= kBlinkIntervalMs;
}}

}}  // namespace {ns}
"""

EXAMPLE_H_NAME = "example.h"
EXAMPLE_CPP_NAME = "example.cpp"

LIBRARIES_README = """# Project libraries

Put board independent source code here in the usual Arduino library layout:

    libraries/
      MySensor/
        library.properties
        src/MySensor.h
        src/MySensor.cpp

`arduino-cli` is invoked with `--libraries "<project>/libraries"` (see the
Console tab for the full command), so any library in this folder is found
before the globally installed ones. Libraries added through the Library
Manager *Install from ZIP* or *Install from Git URL* dialogs end up here too
when the project option is enabled.
"""


class ProjectManager:
    """Creates, opens, validates, renames and deletes Arduino projects."""

    def __init__(self, default_parent: os.PathLike[str] | str | None = None) -> None:
        self._log = get_logger("project")
        self.default_parent = Path(default_parent).expanduser() if default_parent else Path.home() / "Documents"

    # ---------------------------------------------------------------- create
    def suggested_parent(self) -> Path:
        """Best guess for where new projects should go."""
        for candidate in (
            Path.home() / "Documents",
            Path.home() / "Arduino",
            Path.home(),
        ):
            if candidate.is_dir():
                return candidate
        return Path.home()

    def unique_project_dir(self, parent: os.PathLike[str] | str, name: str) -> Path:
        """First free ``<parent>/<name>`` (appends ``-2``, ``-3``, ...)."""
        safe = sanitize_name(name)
        base = Path(parent).expanduser() / safe
        if not base.exists():
            return base
        for index in range(2, 1000):
            candidate = base.with_name(f"{safe}-{index}")
            if not candidate.exists():
                return candidate
        raise ProjectError(f"No free project folder found next to {base}")

    def create_project(
        self,
        parent_dir: os.PathLike[str] | str,
        name: str,
        board_fqbn: str = "arduino:avr:uno",
        description: str = "",
        sketch_source: Optional[str] = None,
        author: str = "",
        with_example_files: bool = True,
    ) -> Project:
        """Create the full project skeleton and return the opened :class:`Project`.

        Raises :class:`ProjectError` when the target already exists or the name
        is unusable.
        """
        if not str(name).strip():
            raise ProjectError("A project name is required.")
        safe_name = sanitize_name(str(name))
        root = Path(parent_dir).expanduser() / safe_name
        if root.exists():
            raise ProjectError(f"A folder called '{root.name}' already exists in {root.parent}.")
        try:
            ensure_dir(root)
            manifest = ProjectManifest(
                name=safe_name,
                main_file=f"{safe_name}.ino",
                board_fqbn=board_fqbn or "arduino:avr:uno",
                description=description,
                author=author,
            )
            sketch = sketch_source if sketch_source is not None else self.default_sketch(safe_name, board_fqbn, description)
            atomic_write_text(root / f"{safe_name}.ino", sketch)
            ensure_dir(root / "include")
            ensure_dir(root / "src")
            ensure_dir(root / "libraries")
            if with_example_files:
                header = root / "include" / EXAMPLE_H_NAME
                source = root / "src" / EXAMPLE_CPP_NAME
                guard = re.sub(r"[^\w]", "_", safe_name.upper()) + "_CONFIG_H"
                ns = re.sub(r"[^\w]", "_", safe_name)
                atomic_write_text(header, EXAMPLE_H_TEMPLATE.format(
                    guard=guard, header=header.name, ns=ns or "project"))
                atomic_write_text(source, EXAMPLE_CPP_TEMPLATE.format(
                    source=source.name, header=header.name, ns=ns or "project"))
            atomic_write_text(root / "libraries" / "README.md", LIBRARIES_README)
            project = Project(root, manifest)
            project.save_manifest()
            self._log.info("created project %s", root)
            return project
        except OSError as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise ProjectError(f"Could not create the project folder: {exc}") from exc

    def default_sketch(self, name: str, board_fqbn: str = "arduino:avr:uno", description: str = "") -> str:
        """The starter ``.ino`` written into a fresh project."""
        from datetime import date

        return DEFAULT_SKETCH_TEMPLATE.format(
            name=name,
            board=board_fqbn,
            date=date.today().isoformat(),
            description=description or "Generated by Arduino Studio",
        )

    def create_project_from_template(
        self,
        parent_dir: os.PathLike[str] | str,
        name: str,
        files: dict[str, str],
        board_fqbn: str = "arduino:avr:uno",
        description: str = "",
        with_example_files: bool = True,
    ) -> Project:
        """Create a project whose content comes from a template mapping.

        *files* maps a project relative path (``"MyProject.ino"``,
        ``"src/util.cpp"``) to its text content.
        """
        safe_name = sanitize_name(name)
        root = self.unique_project_dir(parent_dir, safe_name)
        ensure_dir(root)
        main_file = f"{safe_name}.ino"
        payload = dict(files)
        if main_file not in payload:
            # first .ino in the mapping becomes the main sketch, whatever its name
            for rel in payload:
                if rel.lower().endswith(".ino"):
                    main_file = rel
                    break
            else:
                payload[main_file] = self.default_sketch(safe_name, board_fqbn, description)
        manifest = ProjectManifest(
            name=safe_name,
            main_file=main_file,
            board_fqbn=board_fqbn,
            description=description,
        )
        for rel, text in payload.items():
            target = root / rel
            ensure_dir(target.parent)
            atomic_write_text(target, text)
        for folder in PROJECT_DIRS:
            ensure_dir(root / folder)
        if with_example_files:
            self._write_template_helpers(root, safe_name)
        atomic_write_text(root / "libraries" / "README.md", LIBRARIES_README)
        project = Project(root, manifest)
        project.save_manifest()
        return project

    def _write_template_helpers(self, root: Path, safe_name: str) -> None:
        """Create the scaffold ``include/example.h`` + ``src/example.cpp``."""
        header = root / "include" / EXAMPLE_H_NAME
        source = root / "src" / EXAMPLE_CPP_NAME
        guard = re.sub(r"[^\w]", "_", safe_name.upper()) + "_CONFIG_H"
        ns = re.sub(r"[^\w]", "_", safe_name) or "project"
        if not header.exists():
            atomic_write_text(header, EXAMPLE_H_TEMPLATE.format(
                guard=guard, header=header.name, ns=ns))
        if not source.exists():
            atomic_write_text(source, EXAMPLE_CPP_TEMPLATE.format(
                source=source.name, header=header.name, ns=ns))

    # ------------------------------------------------------------------ open
    def open(self, path: os.PathLike[str] | str) -> Project:
        """Open ``<folder>`` or ``<file.ino>`` as a project (creates a manifest if missing)."""
        target = Path(path).expanduser()
        if target.is_file():
            folder = target.parent
        elif target.is_dir():
            folder = target
        else:
            raise ProjectError(f"'{target}' does not exist.")
        if not is_arduino_folder(folder):
            raise ProjectError(
                f"{folder.name} is not an Arduino sketch folder - no .ino file found inside it. "
                "Open the folder that contains the .ino file, not a parent folder."
            )
        manifest_path = folder / MANIFEST_NAME
        manifest: Optional[ProjectManifest] = None
        if manifest_path.is_file():
            try:
                manifest = ProjectManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                self._log.warning("invalid %s (%s) - rebuilding it", manifest_path, exc)
                manifest = None
        if manifest is None:
            main = folder / f"{folder.name}.ino"
            manifest = ProjectManifest(
                name=folder.name,
                main_file=main.name if main.is_file() else "",
                created=iso_now(),
            )
        if not manifest.main_file or not (folder / manifest.main_file).is_file():
            fallback = folder / f"{folder.name}.ino"
            if fallback.is_file():
                manifest.main_file = fallback.name
            else:
                inos = sorted(folder.glob("*.ino"), key=lambda p: natural_key(p.name))
                if inos:
                    manifest.main_file = inos[0].name
        project = Project(folder, manifest)
        if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8", errors="replace").strip() == "":
            try:
                project.save_manifest()
            except OSError as exc:
                self._log.warning("could not write %s: %s", manifest_path, exc)
        self._log.info("opened project %s", folder)
        return project

    def reload_manifest(self, project: Project) -> ProjectManifest:
        """Re-read ``project.json`` from disk."""
        try:
            data = json.loads(project.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectError(f"Cannot read {project.manifest_path.name}: {exc}") from exc
        project.manifest = ProjectManifest.from_dict(data)
        return project.manifest

    # -------------------------------------------------------------- rename
    def rename_project(self, project: Project, new_name: str) -> tuple[Path, str]:
        """Rename the folder *and* the main ``.ino`` so the sketch stays valid.

        Returns ``(new_root, old_root)`` - the old path is returned so the GUI
        can update window titles, recents and open editors.
        """
        safe = sanitize_name(new_name)
        if safe == project.root.name:
            return project.root, project.root
        old_root = project.root
        new_root = old_root.with_name(safe)
        if new_root.exists():
            raise ProjectError(f"A folder called '{safe}' already exists next to the project.")
        old_main = project.main_sketch
        try:
            if old_main.is_file() and old_main.stem != safe:
                old_main.rename(old_root / f"{safe}.ino")
            old_root.rename(new_root)
        except OSError as exc:
            # try to undo the ino rename so the project is not left broken
            try:
                if old_main.is_file() and (old_root / f"{safe}.ino").is_file():
                    (old_root / f"{safe}.ino").rename(old_main)
            except OSError:
                self._log.exception("rollback after failed rename failed")
            raise ProjectError(f"Could not rename the project: {exc}") from exc

        project.root = new_root
        project.manifest.name = safe
        project.manifest.main_file = f"{safe}.ino"
        project.save_manifest()
        self._log.info("renamed project %s -> %s", old_root, new_root)
        return new_root, old_root

    # -------------------------------------------------------------- delete
    def delete_project(self, project: Project | os.PathLike[str] | str, to_trash: bool = True) -> Path:
        """Delete a project folder, moving it to a backup first when possible.

        ``to_trash`` tries ``send2trash`` (optional dependency) and falls back
        to a zip archive in the parent folder, so a delete is never silently
        destructive.
        """
        root = Path(project.root if isinstance(project, Project) else str(project)).expanduser()
        if not root.is_dir():
            raise ProjectError(f"{root} is not a folder.")
        if not any(root.iterdir()):
            root.rmdir()
            return root
        if to_trash:
            try:  # optional dependency - silently skipped when absent
                from send2trash import send2trash  # type: ignore[import-not-found]

                send2trash(str(root))
                self._log.info("moved project %s to the recycle bin", root)
                return root
            except Exception:
                pass
        archive = root.with_name(f"{root.name}.deleted-{iso_now().replace(':', '').replace('-', '')}")
        try:
            shutil.make_archive(str(archive), "zip", root_dir=str(root.parent), base_dir=str(root.name))
            shutil.rmtree(root)
            self._log.info("deleted %s (backup at %s.zip)", root, archive)
        except OSError as exc:
            raise ProjectError(f"Could not delete the project: {exc}") from exc
        return archive.parent / (archive.name + ".zip")

    def duplicate_project(self, project: Project, new_name: str) -> Project:
        """Copy the whole project next to the original under *new_name*."""
        safe = sanitize_name(new_name) or f"{sanitize_name(project.root.name)}_copy"
        wanted = project.root.parent / safe
        if wanted.exists():
            # the caller gave a name that is taken: keep their wording, add a suffix
            target = self.unique_project_dir(project.root.parent, f"{safe}-copy")
        else:
            target = wanted
        try:
            shutil.copytree(project.root, target, ignore=shutil.ignore_patterns("build", ".git", "*.tmp"))
        except (OSError, shutil.Error) as exc:
            raise ProjectError(f"Could not duplicate the project: {exc}") from exc
        copy = self.open(target)
        copy.manifest.name = target.name
        copy.manifest.main_file = f"{target.name}.ino"
        src_ino = target / f"{project.root.name}.ino"
        if src_ino.is_file():
            src_ino.rename(target / f"{target.name}.ino")
        copy.save_manifest()
        return copy

    # ------------------------------------------------------------- file ops
    def create_file(self, project: Project, relative: str, content: str = "") -> Path:
        """Create a file inside the project (parents are created on demand)."""
        target = project.resolve(relative)
        if target.exists():
            raise ProjectError(f"{project.relative(target)} already exists.")
        ensure_dir(target.parent)
        text = content if content else (self._template_for(target, project) or "")
        atomic_write_text(target, text)
        return target

    def _template_for(self, target: Path, project: Project) -> str:
        """Small starter content for newly created files."""
        stem, suffix = target.stem, target.suffix.lower()
        guard = re.sub(r"[^\w]", "_", f"{project.name}_{stem}".upper()) + "_H"
        rel = project.relative(target)
        if suffix in {".h", ".hpp"}:
            return (
                f"#ifndef {guard}\n#define {guard}\n\n"
                f"// {rel}\n#include <Arduino.h>\n\n\n"
                f"#endif  // {guard}\n"
            )
        if suffix in {".cpp", ".c"}:
            header = target.with_suffix(".h").name
            include = f'#include "{header}"\n' if (target.parent / header).is_file() else "#include <Arduino.h>\n"
            ident = re.sub(r"[^\w]", "_", stem)
            return (
                f"// {rel}\n{include}\n\n"
                f"void {ident}Setup() {{\n}}\n\n"
                f"void {ident}Loop() {{\n}}\n"
            )
        if suffix == ".ino":
            return f"// {rel}\n\nvoid setup() {{}}\n\nvoid loop() {{}}\n"
        if suffix == ".json":
            return "{\n  \n}\n"
        return f"// {rel}\n"

    def create_folder(self, project: Project, relative: str) -> Path:
        target = project.resolve(relative)
        if target.exists():
            raise ProjectError(f"{project.relative(target)} already exists.")
        ensure_dir(target)
        return target

    def delete_file(self, project: Project, relative: str) -> bool:
        """Delete a file or (empty) folder inside the project."""
        target = project.resolve(relative)
        if target == project.root:
            raise ProjectError("The project folder itself cannot be deleted this way.")
        try:
            if target.is_dir():
                if any(target.iterdir()):
                    raise ProjectError(f"{relative} is not empty - delete its files first.")
                target.rmdir()
            elif target.is_file():
                target.unlink()
            else:
                return False
        except OSError as exc:
            raise ProjectError(f"Could not delete {relative}: {exc}") from exc
        return True

    def rename_file(self, project: Project, relative: str, new_name: str) -> Path:
        """Rename a file inside the project, keeping it in the same folder."""
        safe = sanitize_name(new_name)
        old = project.resolve(relative)
        if not old.exists():
            raise ProjectError(f"{relative} does not exist.")
        if "." not in safe and old.suffix:
            safe += old.suffix
        new = old.with_name(safe)
        if new.exists():
            raise ProjectError(f"{new.name} already exists.")
        try:
            old.rename(new)
        except OSError as exc:
            raise ProjectError(f"Could not rename {old.name}: {exc}") from exc
        if project.manifest.main_file and project.relative(old) == project.manifest.main_file:
            if new.suffix.lower() in SKETCH_SUFFIXES:
                project.manifest.main_file = project.relative(new)
                project.save_manifest()
        return new

    def import_file(self, project: Project, source: os.PathLike[str] | str, relative: Optional[str] = None) -> Path:
        """Copy an external file into the project (``src/`` by default)."""
        origin = Path(source).expanduser()
        if not origin.is_file():
            raise ProjectError(f"{origin} does not exist.")
        rel = relative or str(Path("src") / origin.name)
        target = project.resolve(rel)
        if target.exists():
            target = project.resolve(str(Path(rel).with_name(f"{Path(rel).stem}_imported{Path(rel).suffix}")))
        ensure_dir(target.parent)
        shutil.copy2(origin, target)
        return target

    def read_file(self, project: Project, relative: str) -> str:
        target = project.resolve(relative)
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ProjectError(f"Cannot read {relative}: {exc}") from exc

    def write_file(self, project: Project, relative: str, content: str) -> Path:
        target = project.resolve(relative)
        atomic_write_text(target, content)
        return target

    # ------------------------------------------------------- validation
    def validate(self, project: Project) -> list[str]:
        """Return human readable problems (empty list = ready to compile)."""
        problems: list[str] = []
        if not project.root.is_dir():
            return [f"The project folder {project.root} is missing."]
        main = project.main_sketch
        if not main.is_file():
            problems.append(
                f"The main sketch '{project.root.name}.ino' is missing. arduino-cli requires an "
                ".ino file with the same name as the project folder."
            )
        if project.root.name != sanitize_name(project.root.name):
            problems.append(
                "The folder name contains characters the Arduino compiler may reject "
                "(use letters, digits, '_' and '-')."
            )
        if not problems and main.is_file():
            text = main.read_text(encoding="utf-8", errors="replace")
            if "setup" not in text and "loop" not in text:
                problems.append("The main sketch has no setup()/loop() functions.")
        return problems

    def ensure_main_ino(self, project: Project) -> Path:
        """Guarantee ``<folder>.ino`` exists and is registered as the main sketch."""
        expected = project.root / f"{project.root.name}.ino"
        if expected.is_file():
            if project.manifest.main_file != expected.name:
                project.manifest.main_file = expected.name
                project.save_manifest()
            return expected
        candidates: Iterable[Path] = project.sketch_files()
        for candidate in candidates:
            if candidate.name == MANIFEST_NAME:
                continue
            try:
                candidate.rename(expected)
            except OSError as exc:
                raise ProjectError(f"Could not rename {candidate.name} to {expected.name}: {exc}") from exc
            project.manifest.main_file = expected.name
            project.save_manifest()
            return expected
        atomic_write_text(expected, self.default_sketch(project.root.name, project.manifest.board_fqbn))
        project.manifest.main_file = expected.name
        project.save_manifest()
        return expected

    def compile_target(self, project: Project) -> Path:
        """Directory handed to ``arduino-cli compile`` (= the project root)."""
        self.ensure_main_ino(project)
        return project.root

    # ------------------------------------------------------------ recents io
    @staticmethod
    def project_display_path(path: os.PathLike[str] | str) -> str:
        """Shorten the home directory for display (``~/Arduino/Blink``)."""
        p = Path(str(path)).expanduser()
        try:
            return "~/" + p.relative_to(Path.home()).as_posix()
        except ValueError:
            return p.as_posix()

    @staticmethod
    def icon_for(suffix: str | os.PathLike[str]) -> str:
        """Glyph used by the explorer for a file type (no image assets needed).

        Accepts a bare suffix (``".ino"``) or any path/name (``"Blink.ino"``).
        """
        text = str(suffix or "")
        if text and not text.startswith("."):
            text = Path(text).suffix or text
        mapping = {
            ".ino": "ino",
            ".h": "h",
            ".hpp": "hpp",
            ".cpp": "cpp",
            ".c": "c",
            ".json": "json",
            ".txt": "txt",
            ".md": "md",
        }
        return mapping.get(text.lower(), "?")


def flatten_tree(nodes: Iterable[ProjectFile]) -> list[ProjectFile]:
    """Depth first list of all nodes (folders included) - used by search."""
    out: list[ProjectFile] = []
    for node in nodes:
        out.append(node)
        if node.children:
            out.extend(flatten_tree(node.children))
    return out
