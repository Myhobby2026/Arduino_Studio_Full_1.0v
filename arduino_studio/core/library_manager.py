"""Library manager service (registry search, install, updates, ZIP/Git installs).

Three library sources are supported:

``registry``
    ``arduino-cli lib search`` / ``lib install`` - the Arduino Library Manager.
``zip``
    A local ``.zip`` archive containing ``library.properties``.  Global installs
    use ``arduino-cli lib install --zip_path``; project installs are unpacked
    into ``<project>/libraries`` so the project stays self contained.
``git``
    A repository URL.  Global installs use ``arduino-cli lib install --git-url``,
    project installs are cloned with ``git`` into ``<project>/libraries``.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import process as proc
from .arduino_cli import ArduinoCLI, CLIError, CommandError, CommandResult
from .project import Project
from .utils import compare_versions, ensure_dir, get_logger, natural_key

__all__ = [
    "LibraryRecord",
    "LibraryAction",
    "LibraryManager",
    "LibraryError",
    "parse_library_properties",
    "extract_library_zip",
]


class LibraryError(RuntimeError):
    """Raised for library operations that cannot be performed."""


@dataclass
class LibraryRecord:
    """One library, as seen by the Library Manager UI."""

    name: str
    version: str = ""                      # installed version ("" if not installed)
    latest_version: str = ""               # newest version available in the index
    author: str = ""
    maintainer: str = ""
    sentence: str = ""                     # short description
    paragraph: str = ""                    # long description
    category: str = ""
    license: str = ""
    website: str = ""
    repository_url: str = ""
    installed: bool = False
    install_dir: str = ""
    location: str = ""                     # USER | BUILTIN | PROJECT | SKETCHBOOK
    available_versions: list[str] = field(default_factory=list)
    library_id: str = ""
    real_name: str = ""
    architectures: str = ""
    depends: str = ""
    provides_includes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ display
    @property
    def description(self) -> str:
        return self.sentence or self.paragraph or ""

    @property
    def display_name(self) -> str:
        return self.real_name or self.name

    @property
    def scope(self) -> str:
        if self.location.upper() in {"BUILTIN", "CORE"}:
            return "Core (built-in)"
        if self.location.upper() in {"PROJECT", "SKETCHBOOK"}:
            return "Project"
        if self.installed:
            return "Installed (all sketches)"
        return "Available"

    @property
    def update_available(self) -> bool:
        return self.installed and bool(self.latest_version) and compare_versions(
            self.latest_version, self.version
        ) > 0

    @property
    def version_spec(self) -> str:
        """``"Name@1.2.3"`` for ``arduino-cli lib install``."""
        return f"{self.name}@{self.latest_version or self.version}" if (self.latest_version or self.version) else self.name

    @property
    def searchable(self) -> str:
        """Lowercased blob used by the filter box."""
        return " ".join(filter(None, (self.name, self.sentence, self.category, self.author, self.paragraph))).lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LibraryAction:
    """A queued/finished library operation (used for the progress list)."""

    kind: str                 # install | uninstall | update
    record: LibraryRecord
    ok: bool = False
    message: str = ""
    scope: str = "global"     # global | project


def parse_library_properties(path: os.PathLike[str] | str) -> dict[str, str]:
    """Read a ``library.properties`` file into a flat dict."""
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string("[library]\n" + text, encoding="utf-8")
        section = parser["library"]
        return {key.lower(): section[key].strip() for key in section.keys()}
    except Exception:
        pass
    # manual fallback: ``key=value`` lines (Arduino's format is not real ini)
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


def _library_name_from_properties(props: dict[str, str], fallback: str) -> str:
    for key in ("name", "sentence", "architectures"):
        value = props.get(key)
        if key == "name" and value:
            return re.sub(r"[^\w.\- ]+", "", value).strip() or fallback
    return fallback


def extract_library_zip(zip_path: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> Path:
    """Unpack an Arduino library ZIP into *destination* as ``<dest>/<LibraryName>``.

    Handles archives that wrap everything in a single top level folder (the
    GitHub ``Download ZIP`` layout).
    """
    archive = Path(zip_path)
    if not archive.is_file():
        raise LibraryError(f"'{archive.name}' is not a file.")
    if not zipfile.is_zipfile(archive):
        raise LibraryError(f"'{archive.name}' is not a ZIP archive.")
    target_root = Path(destination)
    ensure_dir(target_root)
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise LibraryError("The ZIP archive is empty.")
            top = names[0].split("/", 1)[0] if "/" in names[0] else ""
            prefix = top + "/" if top and all(n.startswith(top + "/") for n in names) else ""
            props_name = next(
                (n for n in names if n.lower().endswith("library.properties") and n[len(prefix):].count("/") <= 1),
                "",
            )
            props: dict[str, str] = {}
            if props_name:
                try:
                    raw = zf.read(props_name).decode("utf-8", errors="replace")
                    props = parse_library_properties_from_text(raw)
                except (KeyError, zipfile.BadZipFile, OSError):
                    props = {}
            name = _library_name_from_properties(props, fallback=archive.stem)
            name = re.sub(r"[^\w.\-]+", "_", name).strip("_") or archive.stem
            target = target_root / name
            if target.exists():
                shutil.rmtree(target)
            ensure_dir(target)
            written = 0
            for member in zf.infolist():
                if member.is_dir():
                    continue
                relative = member.filename[len(prefix):] if prefix else member.filename
                if not relative or relative.startswith("../") or os.path.isabs(relative):
                    continue  # zip-slip / junk guard
                parts = Path(relative).parts
                if any(part in {".", ".."} for part in parts):
                    continue
                out_path = target.joinpath(*parts)
                try:
                    out_path.relative_to(target)
                except ValueError:
                    continue
                ensure_dir(out_path.parent)
                with zf.open(member) as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written += 1
            if written == 0:
                shutil.rmtree(target, ignore_errors=True)
                raise LibraryError("The ZIP did not contain any usable files.")
            return target
    except zipfile.BadZipFile as exc:
        raise LibraryError(f"Corrupt ZIP archive: {exc}") from exc


def parse_library_properties_from_text(text: str) -> dict[str, str]:
    """Parse the content of ``library.properties`` (flat ``key=value``)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


class LibraryManager:
    """High level library operations built on top of :class:`ArduinoCLI`."""

    INDEX_STAMP = "library_index_stamp.txt"

    def __init__(self, cli: ArduinoCLI, data_dir: os.PathLike[str] | str | None = None) -> None:
        self._log = get_logger("libs")
        self.cli = cli
        self.data_dir = Path(data_dir).expanduser() if data_dir else ensure_dir(Path.cwd() / ".arduino_studio")
        self._search_cache: dict[str, tuple[float, list[LibraryRecord]]] = {}
        self._cache_ttl = 60.0 * 10.0

    # ------------------------------------------------------------------ index
    @property
    def index_stamp_path(self) -> Path:
        return self.data_dir / self.INDEX_STAMP

    def index_age_days(self) -> float:
        """How old the local Library Manager index is (``inf`` when never updated)."""
        try:
            stamp = float(self.index_stamp_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return float("inf")
        return max(0.0, (time.time() - stamp) / 86400.0)

    def mark_index_updated(self) -> None:
        try:
            ensure_dir(self.index_stamp_path.parent)
            self.index_stamp_path.write_text(f"{time.time():.3f}", encoding="utf-8")
        except OSError:
            self._log.debug("cannot write index stamp", exc_info=True)

    def index_is_stale(self, max_age_days: int) -> bool:
        return self.index_age_days() > max(0, int(max_age_days))

    def update_index(
        self,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        also_cores: bool = True,
    ) -> CommandResult:
        """``lib update-index`` (+ ``core update-index`` when requested)."""
        result = self.cli.lib_update_index(on_line=on_line, context=context)
        if also_cores:
            second = self.cli.update_index(on_line=on_line, context=context)
            combined_output = (result.output or "") + ("\n" if result.output else "") + (second.output or "")
            result = CommandResult(
                command=[*result.command, *second.command],
                returncode=0 if result.ok and second.ok else (result.returncode or second.returncode),
                output=combined_output,
                duration=result.duration + second.duration,
                cancelled=result.cancelled or second.cancelled,
            )
        if result.returncode == 0:
            self.mark_index_updated()
            self._search_cache.clear()
        return result

    # ---------------------------------------------------------------- search
    def search(self, term: str = "", use_cache: bool = True) -> list[LibraryRecord]:
        """Search the Library Manager index (``arduino-cli lib search``)."""
        query = (term or "").strip()
        cache_key = query.lower()
        if use_cache and query:
            cached = self._search_cache.get(cache_key)
            if cached and (time.time() - cached[0]) < self._cache_ttl:
                return cached[1]
        args = ["lib", "search"]
        if query:
            args.append(query)
        try:
            data = self.cli.execute_json(args, timeout=120.0, ok_required=False)
        except CLIError as exc:
            message = str(exc)
            if "not available" in message.lower() or "index" in message.lower():
                raise LibraryError(
                    "The library index is empty. Run 'Update Index' in the Libraries screen "
                    "(this needs internet access) and try again."
                ) from exc
            raise LibraryError(message) from exc
        records: list[LibraryRecord] = []
        for entry in self._iter_library_entries(data):
            record = self._record_from_cli(entry)
            if record.name:
                records.append(record)
        records.sort(key=lambda r: (not r.installed, natural_key(r.name.lower())))
        if query:
            self._search_cache[cache_key] = (time.time(), records)
        return records

    def installed(self, include_builtins: bool = True, fqbn: str = "") -> list[LibraryRecord]:
        """Libraries currently installed (``arduino-cli lib list``)."""
        args = ["lib", "list"]
        if include_builtins:
            args.append("--all")
        if fqbn:
            args += ["--fqbn", fqbn]
        data = self.cli.execute_json(args, timeout=90.0, ok_required=False)
        return self._records_from_list_payload(data)

    def updatable(self, fqbn: str = "") -> list[LibraryRecord]:
        """Installed libraries that have a newer version in the index."""
        args = ["lib", "list", "--updatable"]
        if fqbn:
            args += ["--fqbn", fqbn]
        data = self.cli.execute_json(args, timeout=90.0, ok_required=False)
        records = self._records_from_list_payload(data)
        for record in records:
            record.installed = True
        return [r for r in records if r.update_available] or records

    def project_libraries(self, project: Project) -> list[LibraryRecord]:
        """Libraries physically inside ``<project>/libraries`` (+ ``src``)."""
        out: list[LibraryRecord] = []
        for base in (project.libraries_dir,):
            if not base.is_dir():
                continue
            for folder in sorted(p for p in base.iterdir() if p.is_dir()):
                props = parse_library_properties(folder / "library.properties")
                name = _library_name_from_properties(props, fallback=folder.name)
                out.append(LibraryRecord(
                    name=name,
                    real_name=folder.name,
                    version=str(props.get("version", "")),
                    author=str(props.get("author", "")),
                    sentence=str(props.get("sentence", "")),
                    paragraph=str(props.get("paragraph", "")),
                    category=str(props.get("category", "")),
                    license=str(props.get("license", "")),
                    website=str(props.get("url", props.get("website", ""))),
                    architectures=str(props.get("architectures", "")),
                    depends=str(props.get("depends", "")),
                    installed=True,
                    install_dir=str(folder),
                    location="PROJECT",
                    provides_includes=self._headers_of(folder),
                ))
        return out

    @staticmethod
    def _headers_of(folder: Path, limit: int = 40) -> list[str]:
        headers: list[str] = []
        for pattern in ("src/*.h", "src/*.hpp", "*.h", "*.hpp", "*/src/*.h"):
            for header in sorted(folder.glob(pattern)):
                if header.name not in headers:
                    headers.append(header.name)
                if len(headers) >= limit:
                    return headers
        return headers

    # --------------------------------------------------------------- actions
    def install(
        self,
        name_spec: str,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        project: Optional[Project] = None,
        project_local: bool = False,
    ) -> CommandResult:
        """Install one library; ``project_local`` is served by :meth:`install_zip`/Git."""
        """Install ``Name`` or ``Name@version`` from the Library Manager index."""
        spec = (name_spec or "").strip()
        if not spec:
            raise LibraryError("No library selected.")
        args = ["lib", "install", spec]
        if project is not None and not project_local:
            # make sure the project's own libraries never shadow a registry install
            args += ["--no-overwrite"]
        result = self.cli.execute(args, on_line=on_line, context=context, timeout=None)
        if result.returncode != 0 and "already installed" in result.output.lower():
            self._log.info("library %s already installed", spec)
        return result

    def install_many(
        self,
        specs: Sequence[str],
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
    ) -> list[tuple[str, CommandResult]]:
        """Install each spec separately so one failure does not abort the rest."""
        outcomes: list[tuple[str, CommandResult]] = []
        for index, spec in enumerate(specs, start=1):
            if context is not None:
                context.progress(index / max(1, len(specs)), f"installing {spec}")
            if on_line is not None:
                on_line(f"[{index}/{len(specs)}] installing {spec}")
            try:
                outcomes.append((spec, self.install(spec, on_line=on_line, context=context)))
            except (CLIError, LibraryError) as exc:
                fake = CommandResult(["lib", "install", spec], 1, str(exc), 0.0)
                outcomes.append((spec, fake))
        return outcomes

    def uninstall(self, name: str, on_line: Optional[Callable[[str], Any]] = None, context: Any = None) -> CommandResult:
        if not name:
            raise LibraryError("No library selected.")
        return self.cli.execute(["lib", "uninstall", name], on_line=on_line, context=context, timeout=None)

    def upgrade_all(self, on_line: Optional[Callable[[str], Any]] = None, context: Any = None) -> CommandResult:
        """``arduino-cli lib upgrade`` - updates every outdated library."""
        return self.cli.execute(["lib", "upgrade"], on_line=on_line, context=context, timeout=None)

    def upgrade_named(
        self,
        names: Sequence[str],
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
    ) -> CommandResult:
        """Update specific libraries (``arduino-cli lib upgrade A B``)."""
        payload = [n for n in names if n]
        if not payload:
            raise LibraryError("No library selected.")
        return self.cli.execute(["lib", "upgrade", *payload], on_line=on_line, context=context, timeout=None)

    def install_zip(
        self,
        zip_path: os.PathLike[str] | str,
        project: Optional[Project] = None,
        project_local: bool = False,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
    ) -> LibraryAction:
        """Install a library from a ZIP file (global or project scoped)."""
        archive = Path(zip_path).expanduser()
        if not archive.is_file():
            raise LibraryError(f"Cannot find '{archive}'.")
        if project_local and project is not None:
            if on_line:
                on_line(f"Unpacking {archive.name} into {project.relative(project.libraries_dir)}/")
            target = extract_library_zip(archive, project.libraries_dir)
            props = parse_library_properties(target / "library.properties")
            record = LibraryRecord(
                name=target.name, version=str(props.get("version", "")), installed=True,
                install_dir=str(target), location="PROJECT",
                sentence=str(props.get("sentence", "")), author=str(props.get("sentence", "")),
                architectures=str(props.get("architectures", "")),
                provides_includes=self._headers_of(target),
            )
            if project is not None:
                from .project import LibraryEntry

                project.add_library(LibraryEntry(
                    name=record.name, version=record.version, source="zip",
                    location=project.relative(target), note=archive.name,
                ))
            return LibraryAction(kind="install", record=record, ok=True, message=f"Installed to {target}", scope="project")
        # arduino-cli renamed this flag: 1.x uses --zip-path, 0.x used --zip_path.
        # Both spellings are tried before falling back to unpacking by hand, so an
        # install never fails just because of a CLI upgrade.
        result = None
        for flag in ("--zip-path", "--zip_path"):
            result = self.cli.execute(
                ["lib", "install", flag, str(archive)], on_line=on_line, context=context, timeout=None
            )
            if result.ok:
                break
            if not self._flag_unsupported(result.output, flag):
                break
        if result is not None and result.ok:
            record = self._record_from_zip_name(archive)
            return LibraryAction(kind="install", record=record, ok=True, message="Installed globally",
                                 scope="global")
        if on_line and result is not None and self._unsafe_install_disabled(result.output):
            on_line("arduino-cli blocks ZIP installs unless 'library.enable_unsafe_install: true' is set in "
                    "its config - extracting into the libraries folder directly instead.")
        sketchbook = self.cli.sketchbook_dir / "libraries"
        if on_line:
            on_line(f"Unpacking {archive.name} into {sketchbook}")
        try:
            target = extract_library_zip(archive, sketchbook)
        except LibraryError as exc:
            detail = self.cli.humanize_failure(getattr(result, "output", "") or "") if result is not None else ""
            return LibraryAction(kind="install", record=LibraryRecord(name=archive.stem), ok=False,
                                 message=f"{exc}{' ' + detail if detail else ''}", scope="global")
        props = parse_library_properties(target / "library.properties")
        record = LibraryRecord(
            name=target.name, version=str(props.get("version", "")), installed=True, install_dir=str(target),
            location="USER", sentence=str(props.get("sentence", "")),
            provides_includes=self._headers_of(target),
        )
        return LibraryAction(kind="install", record=record, ok=True,
                             message=f"Installed to {target} (unpacked by Arduino Studio)", scope="global")

    def install_git(
        self,
        url: str,
        project: Optional[Project] = None,
        project_local: bool = False,
        branch: str = "",
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
    ) -> LibraryAction:
        """Install a library from a Git URL (``git clone`` or ``--git-url``)."""
        url = (url or "").strip()
        if not re.match(r"^(https?://|git://|git@|file://)", url):
            raise LibraryError("Enter a Git URL, e.g. https://github.com/user/Arduino_Library.git")
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]) or "library"
        if project_local and project is not None:
            git_exe = proc.find_executable("git")
            if not git_exe:
                raise LibraryError(
                    "Git is not installed, so a project local clone is not possible.\n"
                    "Install Git for Windows, or download the repository as ZIP and use "
                    "'Install from ZIP' instead."
                )
            target = project.libraries_dir / name
            if target.exists():
                shutil.rmtree(target)
            args = [git_exe, "clone", "--depth", "1"]
            if branch:
                args += ["--branch", branch]
            args += [url, str(target)]
            result = proc.run_streaming(args, on_line=on_line, context=context, timeout=600.0)
            if result.returncode != 0:
                shutil.rmtree(target, ignore_errors=True)
                raise LibraryError(f"git clone failed: {self.cli.humanize_failure(result.output) or result.output[-300:]}")
            props = parse_library_properties(target / "library.properties")
            record = LibraryRecord(
                name=_library_name_from_properties(props, fallback=name), version=str(props.get("version", "")),
                install_dir=str(target), installed=True, location="PROJECT",
                repository_url=url, sentence=str(props.get("sentence", "")),
                provides_includes=self._headers_of(target),
            )
            from .project import LibraryEntry

            project.add_library(LibraryEntry(
                name=record.name, version=record.version, source="git", url=url,
                location=project.relative(target), note="cloned",
            ))
            return LibraryAction(kind="install", record=record, ok=True, message=f"Cloned into {target}", scope="project")
        # The CLI takes the branch/tag/commit as a URL fragment: --git-url <url>#<ref>
        candidates = [f"{url}#{branch}" if branch else url, url]
        result = None
        for index, target_url in enumerate(candidates):
            result = self.cli.execute(["lib", "install", "--git-url", target_url],
                                      on_line=on_line, context=context, timeout=None)
            if result.ok:
                if index and on_line:
                    on_line("The requested branch was refused; the default branch was installed instead.")
                break
        ok = bool(result is not None and result.ok)
        message = "Installed globally" if ok else self.cli.humanize_failure(
            getattr(result, "output", "") or "") or "arduino-cli refused the Git install."
        if not ok and result is not None and self._unsafe_install_disabled(result.output):
            message = ("arduino-cli only installs from Git when 'library.enable_unsafe_install: true' is set in "
                       f"its config file.\n\n{message}")
        return LibraryAction(
            kind="install",
            record=LibraryRecord(name=name, repository_url=url, installed=ok),
            ok=ok, message=message, scope="global",
        )

    @staticmethod
    def _unsafe_install_disabled(output: str) -> bool:
        """True when arduino-cli refused because unsafe installs are switched off."""
        lowered = (output or "").lower()
        return "unsafe" in lowered and ("enable_unsafe_install" in lowered or "not allowed" in lowered)

    @staticmethod
    def _flag_unsupported(output: str, flag: str) -> bool:
        """True when the CLI does not know *flag* (older/newer spelling)."""
        lowered = (output or "").lower()
        return ("unknown flag" in lowered or "unrecognized" in lowered or "invalid argument" in lowered
                or f"flag {flag}" in lowered or flag.lower() in lowered and "not" in lowered)

    def remove_project_library(self, project: Project, record: LibraryRecord) -> bool:
        """Delete a folder from ``<project>/libraries`` (and its manifest entry)."""
        target = Path(record.install_dir or (project.libraries_dir / record.name))
        try:
            resolved = target.resolve()
            if project.libraries_dir.resolve() not in resolved.parents and resolved != project.libraries_dir.resolve():
                raise LibraryError("That library is not inside the project libraries folder.")
            if resolved.is_dir():
                shutil.rmtree(resolved)
            project.remove_library(record.real_name or record.name)
            return True
        except OSError as exc:
            raise LibraryError(f"Could not delete {target}: {exc}") from exc

    # ------------------------------------------------------------- lookups
    def find_installed(self, name: str) -> Optional[LibraryRecord]:
        needle = (name or "").lower()
        if not needle:
            return None
        for record in self.installed(include_builtins=True):
            if record.name.lower() == needle or record.real_name.lower() == needle:
                return record
        return None

    def resolve_includes(self, headers: Iterable[str]) -> dict[str, LibraryRecord]:
        """Map missing include names to the library that provides them."""
        wanted = [h for h in headers if h]
        mapping: dict[str, LibraryRecord] = {}
        if not wanted:
            return mapping
        installed = self.installed(include_builtins=True)
        by_header = {header.lower(): record for record in installed for header in record.provides_includes}
        still_missing: list[str] = []
        for header in wanted:
            record = by_header.get(header.lower())
            if record is not None:
                mapping[header] = record
            else:
                still_missing.append(header)
        if still_missing:
            suggestions = self.cli.suggest_libraries_for_headers(still_missing)
            for header, lib_name in suggestions.items():
                record = LibraryRecord(name=lib_name, real_name=lib_name)
                try:
                    for candidate in self.search(lib_name)[:5]:
                        if candidate.name.lower() == lib_name.lower():
                            record = candidate
                            break
                except LibraryError:
                    pass
                mapping[header] = record
        return mapping

    def install_missing_for_project(
        self,
        project: Project,
        headers: Sequence[str],
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        project_local: bool = False,
    ) -> list[LibraryAction]:
        """Install the libraries that provide *headers* (used by the error prompt)."""
        mapping = self.resolve_includes(headers)
        actions: list[LibraryAction] = []
        for header, record in mapping.items():
            if record.installed:
                if on_line:
                    on_line(f"{header}: provided by already installed '{record.name}' {record.version}".strip())
                continue
            if on_line:
                on_line(f"Installing '{record.name}' for include <{header}>")
            try:
                result = self.install(record.name, on_line=on_line, context=context,
                                       project=project, project_local=project_local)
                actions.append(LibraryAction(
                    kind="install", record=record, ok=result.returncode == 0,
                    message="installed" if result.ok else self.cli.humanize_failure(result.output) or "failed",
                    scope="project" if project_local else "global",
                ))
            except (CLIError, LibraryError) as exc:
                actions.append(LibraryAction(kind="install", record=record, ok=False, message=str(exc)))
        return actions

    # -------------------------------------------------------------- internals
    @staticmethod
    def _iter_library_entries(data: Any) -> Iterable[dict[str, Any]]:
        if isinstance(data, dict):
            for key in ("searched_libraries", "libraries", "installed_and_update_available_libraries"):
                value = data.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            yield item.get("library") if isinstance(item.get("library"), dict) else item
                    return
            for value in data.values():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            yield item.get("library") if isinstance(item.get("library"), dict) else item
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item.get("library") if isinstance(item.get("library"), dict) else item

    def _records_from_list_payload(self, data: Any) -> list[LibraryRecord]:
        records: list[LibraryRecord] = []
        for entry in self._iter_library_entries(data):
            record = self._record_from_cli(entry)
            if not record.latest_version:
                latest = entry.get("latest_version") or entry.get("latest") or {}
                if isinstance(latest, dict):
                    record.latest_version = str(latest.get("version") or "")
                elif isinstance(latest, str):
                    record.latest_version = latest
            if not record.installed:
                record.installed = True
            records.append(record)
        records.sort(key=lambda r: natural_key(r.name.lower()))
        return records

    @staticmethod
    def _record_from_cli(entry: dict[str, Any]) -> LibraryRecord:
        def get(*keys: str, default: str = "") -> str:
            for key in keys:
                value = entry.get(key)
                if value not in (None, "", [], {}):
                    return str(value)
            return default

        def get_list(key: str) -> list[str]:
            value = entry.get(key)
            if isinstance(value, list):
                return [str(v) for v in value if str(v).strip()]
            if isinstance(value, str) and value:
                return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
            return []

        versions_raw = entry.get("versions") or []
        versions: list[str] = []
        latest = ""
        if isinstance(versions_raw, list):
            for item in versions_raw:
                if isinstance(item, dict):
                    version = str(item.get("version") or "")
                    if version:
                        versions.append(version)
                elif isinstance(item, str) and item:
                    versions.append(item)
        latest_obj = entry.get("latest")
        if isinstance(latest_obj, dict):
            latest = str(latest_obj.get("version") or "")
        elif isinstance(latest_obj, str):
            latest = latest_obj
        if not latest and versions:
            latest = sorted(versions, key=lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)])[-1]
        installed_value = entry.get("installed")
        installed = bool(installed_value) if not isinstance(installed_value, str) else installed_value.strip().lower() not in {"false", "0", "no", ""}
        repo = entry.get("repository")
        repo_url = str(repo.get("url")) if isinstance(repo, dict) else (str(repo) if repo else "")
        website = get("website", "url")
        includes = get_list("provides_includes") or get_list("includes")
        return LibraryRecord(
            name=get("name", "real_name"),
            version=get("version", "installed"),
            latest_version=latest,
            author=get("author", "maintainer"),
            maintainer=get("maintainer"),
            sentence=get("sentence"),
            paragraph=get("paragraph", "description"),
            category=get("category", "categories"),
            license=get("license", "type"),
            website=website,
            repository_url=repo_url,
            installed=installed,
            install_dir=get("install_dir"),
            location=get("location"),
            available_versions=versions,
            library_id=get("id"),
            real_name=get("real_name"),
            architectures=get("architectures"),
            depends=get("depends"),
            provides_includes=includes,
        )

    def _record_from_zip_name(self, archive: Path) -> LibraryRecord:
        try:
            with zipfile.ZipFile(archive) as zf:
                candidate = next((n for n in zf.namelist() if n.lower().endswith("library.properties")), "")
                props = parse_library_properties_from_text(zf.read(candidate).decode("utf-8", errors="replace")) if candidate else {}
        except (OSError, zipfile.BadZipFile):
            props = {}
        return LibraryRecord(
            name=_library_name_from_properties(props, fallback=archive.stem),
            version=str(props.get("version", "")),
            sentence=str(props.get("sentence", "")),
            installed=True,
        )


def summarise_records(records: Sequence[LibraryRecord]) -> str:
    """One-line status text such as ``"12 libraries, 3 updatable"``."""
    updatable = sum(1 for r in records if r.update_available)
    installed = sum(1 for r in records if r.installed)
    return f"{len(records)} shown - {installed} installed" + (f", {updatable} updatable" if updatable else "")
