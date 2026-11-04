"""Service discovery: work out, from the code itself, what a service is
called and how other code refers to it -- so no one has to hand-write the
`--services` manifest.

For one service this finds
  - its **name**: Spring's `spring.application.name`, else the Maven
    artifactId / Gradle `rootProject.name`, else the Python project name,
    else the npm package name, else the folder name;
  - its **import names**: the identifiers other code uses to import it --
    top-level Python packages, the npm package name, the Java package
    prefix shared by all its sources.

A *registry* is the list of such services across a set of repos. It turns
into the manifest the parsers consume (`build_manifest`): every other
service's import names map to its name (an import is a high-confidence
edge), and a `://<name>` pattern maps URLs like `http://payments-service/`
or `lb://payments-service` to it.

Guards against false edges: generic or standard-library import names
("utils", "config", "json"...) are never registered, and an import name
claimed by two services is dropped (it cannot identify either).
"""

from __future__ import annotations

import configparser
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from depatlas.fsutil import SKIP_DIRS, detect_languages, iter_files

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

# Import names too generic to identify one service.
GENERIC_IMPORT_NAMES = frozenset(
    {
        "app", "src", "test", "tests", "utils", "util", "common", "config", "core", "lib",
        "main", "models", "api", "web", "server", "client", "base", "service", "services",
        "scripts", "tools", "docs", "migrations", "settings", "setup", "conftest", "manage",
        "types", "index", "shared", "helpers", "handlers", "routes", "db", "database",
        "lambda_function", "handler", "function",
    }
)
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_NON_SERVICE_PY = {"setup.py", "conftest.py", "manage.py", "wsgi.py", "asgi.py", "noxfile.py"}
_MIN_JAVA_PACKAGE_SEGMENTS = 3


@dataclass
class DiscoveredService:
    name: str
    path: str  # relative to the workspace root ("." for the root itself)
    languages: list[str] = field(default_factory=list)
    import_names: list[str] = field(default_factory=list)
    source: str = ""  # where the name came from, e.g. "application.yml"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "languages": self.languages,
            "import_names": self.import_names,
            "source": self.source,
        }


# ---- names ----


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip("'\"").strip()
    if not value or "${" in value or "{{" in value:
        return None  # a placeholder, not a name
    return value


def _spring_name_from_doc(doc: object) -> str | None:
    """`spring.application.name` from one parsed YAML document, nested
    (`spring: {application: {name: x}}`) or flat (`spring.application.name: x`).
    A document that only applies under some profile is ignored.
    """
    if not isinstance(doc, dict):
        return None
    if doc.get("spring.config.activate.on-profile") or doc.get("spring.profiles"):
        return None
    spring = doc.get("spring")
    if isinstance(spring, dict):
        activate = spring.get("config", {})
        if isinstance(activate, dict) and activate.get("activate"):
            return None
        if spring.get("profiles"):
            return None
        app = spring.get("application")
        if isinstance(app, dict):
            name = _clean(app.get("name"))
            if name:
                return name
    return _clean(doc.get("spring.application.name"))


def _spring_config_files(resources: Path) -> list[Path]:
    """bootstrap.* and application.* first, then profile files
    (application-dev.yml...), which only override the base for one profile
    and so must never decide the service's name.
    """
    files = [f for f in resources.iterdir() if f.is_file()]
    base = [f for f in files if f.stem in ("bootstrap", "application")]
    profiles = [f for f in files if f.stem.startswith(("bootstrap-", "application-"))]
    return sorted(base, key=lambda f: (f.stem != "bootstrap", f.name)) + sorted(
        profiles, key=lambda f: f.name
    )


def _spring_application_name(path: Path) -> str | None:
    resources = path / "src" / "main" / "resources"
    if not resources.is_dir():
        return None
    for file in _spring_config_files(resources):
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if file.suffix in (".yml", ".yaml"):
            try:
                documents = list(yaml.safe_load_all(text))
            except yaml.YAMLError:
                continue
            for doc in documents:
                name = _spring_name_from_doc(doc)
                if name:
                    return name
        elif file.suffix == ".properties":
            match = re.search(r"^\s*spring\.application\.name\s*[=:]\s*(.+?)\s*$", text, re.M)
            name = _clean(match.group(1)) if match else None
            if name:
                return name
    return None


def _pom(path: Path) -> tuple[str | None, bool]:
    """(artifactId, has_modules) of a Maven pom.xml."""
    pom = path / "pom.xml"
    if not pom.is_file():
        return None, False
    try:
        data = pom.read_bytes()
        if b"<!ENTITY" in data:  # entity expansion: never parse these
            return None, False
        root = ET.fromstring(data)
    except (ET.ParseError, OSError):
        return None, False
    artifact = None
    has_modules = False
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "artifactId":
            artifact = _clean(child.text)
        elif tag == "modules":
            has_modules = True
    return artifact, has_modules


def _gradle_name(path: Path) -> str | None:
    for settings in ("settings.gradle", "settings.gradle.kts"):
        file = path / settings
        if file.is_file():
            try:
                match = re.search(
                    r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", file.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                return None
            if match:
                return _clean(match.group(1))
    return None


def _toml_name(text: str, tables: tuple[tuple[str, ...], ...]) -> str | None:
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        for table in tables:
            node = data
            for key in table:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            name = _clean(node.get("name")) if isinstance(node, dict) else None
            if name:
                return name
        return None
    for table in tables:  # Python 3.10: read `name = "..."` inside the right [table]
        header = r"^\[" + re.escape(".".join(table)) + r"\]\s*$"
        match = re.search(header + r"(.*?)(?=^\[|\Z)", text, re.M | re.S)
        if match:
            name = re.search(r"^name\s*=\s*['\"]([^'\"]+)['\"]", match.group(1), re.M)
            if name:
                return _clean(name.group(1))
    return None


def _python_name(path: Path) -> str | None:
    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        try:
            name = _toml_name(
                pyproject.read_text(encoding="utf-8"), (("project",), ("tool", "poetry"))
            )
        except (OSError, UnicodeDecodeError):
            name = None
        if name:
            return name
    cfg = path / "setup.cfg"
    if cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(cfg.read_text(encoding="utf-8"))
            name = _clean(parser.get("metadata", "name", fallback=None))
        except (OSError, UnicodeDecodeError, configparser.Error):
            name = None
        if name:
            return name
    setup = path / "setup.py"
    if setup.is_file():
        try:
            text = setup.read_text(encoding="utf-8")
            match = re.search(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", text[max(text.find("setup("), 0):])
        except (OSError, UnicodeDecodeError):
            match = None
        if match:
            return _clean(match.group(1))
    return None


def _package_json(path: Path) -> dict | None:
    file = path / "package.json"
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def infer_service_name(path: Path) -> tuple[str, str]:
    """(name, where it came from) for the service at `path`. Always returns
    something: the folder name is the last resort.
    """
    path = Path(path).resolve()
    spring = _spring_application_name(path)
    if spring:
        return spring, "spring.application.name"
    artifact, _ = _pom(path)
    if artifact:
        return artifact, "pom.xml"
    gradle = _gradle_name(path)
    if gradle:
        return gradle, "settings.gradle"
    python = _python_name(path)
    if python:
        return python, "pyproject.toml/setup"
    package = _package_json(path)
    npm = _clean(package.get("name")) if package else None
    if npm:
        return npm.split("/")[-1], "package.json"  # "@acme/pay" -> "pay"
    return path.name, "folder name"


# ---- import names ----


def _python_import_names(path: Path, name: str) -> list[str]:
    found: list[str] = []
    for base in (path, path / "src"):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name in SKIP_DIRS or entry.name.startswith((".", "_")):
                continue
            if entry.is_dir() and (entry / "__init__.py").is_file():
                found.append(entry.name)
            elif base == path and entry.suffix == ".py" and entry.name not in _NON_SERVICE_PY:
                found.append(entry.stem)
    if not found:
        found.append(re.sub(r"[-.\s]+", "_", name).lower())
    return [n for n in dict.fromkeys(found) if n.isidentifier()]


def _java_package_prefix(path: Path) -> str | None:
    """The longest dotted package that every main source file lives under."""
    source_root = path / "src" / "main"
    packages = []
    for file in iter_files(source_root, "*.java") if source_root.is_dir() else ():
        try:
            head = file.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        match = re.search(r"^\s*package\s+([\w.]+)\s*;", head, re.M)
        if match:
            packages.append(match.group(1).split("."))
    if not packages:
        return None
    prefix = packages[0]
    for pkg in packages[1:]:
        i = 0
        while i < min(len(prefix), len(pkg)) and prefix[i] == pkg[i]:
            i += 1
        prefix = prefix[:i]
    if len(prefix) < _MIN_JAVA_PACKAGE_SEGMENTS:
        return None  # too short to identify one service ("org.springframework")
    return ".".join(prefix)


def _usable_import_name(name: str, language: str) -> bool:
    if language == "python":
        return name.lower() not in GENERIC_IMPORT_NAMES and name not in _STDLIB
    if language == "javascript":
        return name.lower() not in GENERIC_IMPORT_NAMES
    return True


def describe_service(path: Path, root: Path | None = None, name: str | None = None) -> DiscoveredService:
    """Everything discoverable about the service whose code is at `path`."""
    path = Path(path).resolve()
    inferred, source = infer_service_name(path)
    service_name = name or inferred
    if name:
        source = "configured"
    languages = detect_languages(path)

    import_names: list[str] = []
    if "python" in languages:
        import_names += [n for n in _python_import_names(path, service_name) if _usable_import_name(n, "python")]
    if "javascript" in languages or _package_json(path):
        package = _package_json(path)
        npm = _clean(package.get("name")) if package else None
        if npm and _usable_import_name(npm.split("/")[-1], "javascript"):
            import_names.append(npm)
    if "java" in languages:
        prefix = _java_package_prefix(path)
        if prefix:
            import_names.append(prefix)

    rel = "."
    if root is not None:
        try:
            rel = path.relative_to(Path(root).resolve()).as_posix() or "."
        except ValueError:
            rel = str(path)
    return DiscoveredService(
        name=service_name,
        path=rel,
        languages=languages,
        import_names=list(dict.fromkeys(import_names)),
        source=source,
    )


# ---- workspace walk ----


def _is_service_dir(path: Path) -> bool:
    """A folder that holds one deployable service: it has a project marker,
    is not just an aggregator (a Maven parent pom with <modules>, an npm
    `workspaces` root), and actually contains source code.
    """
    artifact, has_modules = _pom(path)
    has_pom = (path / "pom.xml").is_file()
    has_gradle = any((path / f).is_file() for f in ("build.gradle", "build.gradle.kts"))
    if (has_pom and not has_modules) or has_gradle:
        if any(iter_files(path / "src", "*.java")) if (path / "src").is_dir() else False:
            return True
    package = _package_json(path)
    if package is not None and "workspaces" not in package:
        if any(detect_languages(path)):
            return True
    if any((path / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        pyproject = path / "pyproject.toml"
        try:
            workspace = pyproject.is_file() and "[tool.uv.workspace]" in pyproject.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            workspace = False
        if not workspace and "python" in detect_languages(path):
            return True
    return False


def discover_workspace(root: Path, max_depth: int = 3) -> list[DiscoveredService]:
    """Find every service under `root` (the root itself included)."""
    root = Path(root).resolve()
    services: list[DiscoveredService] = []
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and depth < max_depth
        )
        if _is_service_dir(current):
            services.append(describe_service(current, root))
    return services


# ---- registry <-> manifest ----


def registry_to_dict(services: list[DiscoveredService]) -> dict:
    return {"services": [s.to_dict() for s in services]}


def load_registry(path: Path) -> list[dict]:
    """Registry entries from a `discover` file, or from a graph export
    that carries one under "registry" (what `scan-all --export` writes).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    entries = None
    if isinstance(data, dict):
        entries = data.get("registry", data.get("services"))
        if isinstance(entries, dict):  # {"registry": {"services": [...]}}
            entries = entries.get("services")
    elif isinstance(data, list):
        entries = data
    if not isinstance(entries, list) or not all(isinstance(e, dict) and "name" in e for e in entries):
        raise ValueError(f"{path} has no service registry (expected the output of `depatlas discover`)")
    return entries


def _name_key(name: str) -> str:
    """The key a service's own name goes under in the manifest. A name like
    "email" or "config" would otherwise turn every `import email` in other
    code into a dependency, so such names get a key no import can equal;
    the service still takes part in host-name matching, which reads values.
    """
    if name.lower() in GENERIC_IMPORT_NAMES or name in _STDLIB:
        return f"service:{name}"
    return name


def build_manifest(
    self_name: str, registry: list[dict]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """The (known_services, known_endpoints, warnings) a parser needs, for
    scanning the service `self_name` given every service in the registry.
    """
    known_services: dict[str, str] = {"__self__": self_name}
    known_endpoints: dict[str, str] = {}
    warnings: list[str] = []

    claimed: dict[str, set[str]] = {}
    for entry in registry:
        if entry["name"] == self_name:
            continue
        for imp in entry.get("import_names", []):
            claimed.setdefault(imp, set()).add(entry["name"])

    for imp, owners in sorted(claimed.items()):
        if len(owners) > 1:
            warnings.append(
                f"import name '{imp}' is claimed by {', '.join(sorted(owners))}; ignoring it"
            )
        else:
            known_services[imp] = next(iter(owners))

    for entry in registry:
        name = entry["name"]
        if name == self_name:
            continue
        known_services.setdefault(_name_key(name), name)
        known_endpoints[f"://{name}"] = name
    return known_services, known_endpoints, warnings


def find_codeowners(repo_path: Path) -> Path | None:
    """A CODEOWNERS file in one of the standard places, else None."""
    for location in CODEOWNERS_LOCATIONS:
        candidate = Path(repo_path) / location
        if candidate.is_file():
            return candidate
    return None


def _registry_name_for_ci_repo(registry: list[dict]) -> str | None:
    """In GitHub Actions, the registry entry for the repository being built
    (`scan-all` records GitHub repos as `github:owner/repo`), so a renamed
    service (config `name:`) is still recognised without `--name`.
    """
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip().lower()
    if not repository:
        return None
    for entry in registry:
        if str(entry.get("path", "")).lower() == f"github:{repository}":
            return entry["name"]
    return None


def resolve_manifest(
    repo_path: Path,
    services_path: Path | None,
    registry_path: Path | None,
    name: str | None,
    log=lambda message: None,
) -> tuple[dict[str, str], dict[str, str]]:
    """The (known_services, known_endpoints) for scanning `repo_path`.

    A hand-written `--services` file works exactly as before. Without one,
    the service is named from the code (or `--name`), and a `--registry`
    (from `discover`, or a graph export carrying one) supplies the other
    services. With both, the file wins wherever it says something.
    """
    explicit: dict = {}
    if services_path is not None:
        explicit = json.loads(Path(services_path).read_text(encoding="utf-8"))
    explicit_endpoints = explicit.pop("endpoints", {})

    if services_path is not None and registry_path is None and name is None:
        return explicit, explicit_endpoints  # the original, fully explicit behaviour

    registry = load_registry(registry_path) if registry_path is not None else []

    self_name = name or explicit.get("__self__")
    if not self_name:
        self_name = _registry_name_for_ci_repo(registry)
        if self_name:
            log(f"Service name: {self_name} (matched {os.environ.get('GITHUB_REPOSITORY')} in the registry)")
    if not self_name:
        self_name, source = infer_service_name(repo_path)
        log(f"Service name: {self_name} (from {source}; override with --name)")
    if registry and self_name not in {e["name"] for e in registry}:
        known = sorted(e["name"] for e in registry)
        shown = ", ".join(known[:8]) + (", ..." if len(known) > 8 else "")
        log(
            f"warning: '{self_name}' is not in the registry ({shown}). Other services' "
            "dependents are looked up by this name, so if the scan config gives this "
            "repo a different 'name:', pass --name."
        )

    known_services, known_endpoints, warnings = build_manifest(self_name, registry)
    for warning in warnings:
        log(f"warning: {warning}")

    explicit.pop("__self__", None)
    known_services.update(explicit)
    known_endpoints.update(explicit_endpoints)
    return known_services, known_endpoints
