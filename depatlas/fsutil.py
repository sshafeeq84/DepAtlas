"""Filesystem helpers shared by the parsers and the ownership walk."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterator

# Folders that hold vendored, generated or VCS data -- never the
# service's own source. Walking into node_modules alone can mean
# thousands of third-party files, all of which would be parsed and
# wrongly attributed as this service's dependencies.
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "dist", "build", "vendor", "__pycache__", ".tox"}
)


# Hand-written source is far below this; bigger files are minified bundles,
# data dumps or generated code, and parsing them is slow and meaningless.
MAX_SOURCE_BYTES = 2_000_000
_warned_large: set[str] = set()


def iter_files(root: Path, pattern: str = "*") -> Iterator[Path]:
    """Like root.rglob(pattern) for files, but never descends into SKIP_DIRS
    and skips files over MAX_SOURCE_BYTES (with a warning).
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            # os.walk lists a broken symlink as a file; opening it would fail.
            if not (fnmatch.fnmatch(name, pattern) and path.is_file()):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_SOURCE_BYTES:
                if str(path) not in _warned_large:
                    _warned_large.add(str(path))
                    warn_skipped(path, OSError(f"file is larger than {MAX_SOURCE_BYTES // 1_000_000} MB"))
                continue
            yield path


def warn_skipped(path: Path, reason: Exception) -> None:
    import sys

    print(f"warning: skipping {path} ({type(reason).__name__}: {reason})", file=sys.stderr)


LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".mts": "javascript",
    ".cts": "javascript",
    ".tsx": "javascript",
}


def language_of(rel: str | Path) -> str | None:
    """The DepAtlas language name for a file, from its extension."""
    return LANGUAGE_EXTENSIONS.get(Path(rel).suffix.lower())


def detect_languages(root: Path) -> list[str]:
    """Languages present in a repo, by file extension (sorted)."""
    found = set()
    for path in iter_files(root):
        lang = language_of(path)
        if lang:
            found.add(lang)
    return sorted(found)


def is_test_path(rel: Path) -> bool:
    """True for files that hold tests, not the service's public interface.
    Deleting or renaming a test must never look like a breaking API change.
    The rules are per language, so a production package that merely happens
    to be called `test` (com/org/test/) is not mistaken for tests.
    """
    rel = Path(rel)
    parts = rel.parts
    name = rel.name
    lang = language_of(rel)
    stem = rel.stem
    if lang == "java":
        return (
            ("src" in parts and "test" in parts[parts.index("src") + 1 : parts.index("src") + 2])
            or stem.endswith(("Test", "Tests", "IT"))
            or stem.startswith("Test")
        )
    if lang == "python":
        return (
            bool({"tests", "test"} & set(parts[:-1]))
            or name.startswith("test_")
            or stem.endswith("_test")
            or name == "conftest.py"
        )
    if lang == "javascript":
        return "__tests__" in parts[:-1] or any(
            f".{kind}." in name for kind in ("test", "spec")
        )
    return False


def relative_to_repo(path: str | Path, repo_path: Path) -> str:
    """A path relative to the repo root, with forward slashes. Stored paths
    must not depend on the directory a scan ran from, or `diff` reports every
    edge as moved whenever the repo is scanned from somewhere else.
    """
    try:
        return Path(path).relative_to(repo_path).as_posix()
    except ValueError:
        return str(path)
