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


def iter_files(root: Path, pattern: str = "*") -> Iterator[Path]:
    """Like root.rglob(pattern) for files, but never descends into SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if fnmatch.fnmatch(name, pattern):
                yield Path(dirpath) / name


def warn_skipped(path: Path, reason: Exception) -> None:
    import sys

    print(f"warning: skipping {path} ({type(reason).__name__}: {reason})", file=sys.stderr)


def is_test_path(rel: Path) -> bool:
    """True for files that hold tests, not the service's public interface.
    Deleting or renaming a test must never look like a breaking API change.
    """
    name = rel.name
    return bool({"test", "tests", "__tests__"} & set(rel.parts[:-1])) or (
        name.startswith("test_")
        or name.endswith(
            (
                "_test.py",
                "Test.java",
                "Tests.java",
                ".test.js",
                ".spec.js",
                ".test.jsx",
                ".spec.jsx",
                ".test.ts",
                ".spec.ts",
                ".test.tsx",
                ".spec.tsx",
            )
        )
    )


def relative_to_repo(path: str | Path, repo_path: Path) -> str:
    """A path relative to the repo root, with forward slashes. Stored paths
    must not depend on the directory a scan ran from, or `diff` reports every
    edge as moved whenever the repo is scanned from somewhere else.
    """
    try:
        return Path(path).relative_to(repo_path).as_posix()
    except ValueError:
        return str(path)
