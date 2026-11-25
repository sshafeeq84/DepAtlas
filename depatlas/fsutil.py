"""Filesystem helpers shared by the parsers and the ownership walk."""

from __future__ import annotations

import fnmatch
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

# Folders that hold vendored, generated or VCS data -- never the
# service's own source. Walking into node_modules alone can mean
# thousands of third-party files, all of which would be parsed and
# wrongly attributed as this service's dependencies.
SKIP_DIRS = frozenset(
    {
        ".git", "node_modules", ".venv", "venv", "dist", "build", "vendor", "__pycache__", ".tox",
        ".depatlas",  # DepAtlas's own cache and databases: never scan what it downloaded
    }
)


# Hand-written source is far below this; bigger files are minified bundles,
# data dumps or generated code, and parsing them is slow and meaningless.
MAX_SOURCE_BYTES = 2_000_000
_warned_large: set[str] = set()


def path_globs(pattern: str) -> list[str]:
    """The fnmatch globs that together express one gitignore-style pattern
    (as used by CODEOWNERS and by exclude rules).

    A pattern with a "/" at the start or in the middle is anchored to the
    root, any other matches at any depth; a trailing "/" means everything
    inside that folder; a bare name matches a file or a folder of that name
    (and what is inside it). Negation and "**" are out of scope. fnmatch's
    "*" also crosses "/", which is more generous than gitignore.
    """
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.lstrip("/")
    if body.endswith("/"):
        body += "*"
    globs = [body] if "*" in body else [body, body + "/*"]
    return globs if anchored else globs + ["*/" + g for g in globs]


# Active exclusions: (tree root, globs). Set with `excluding()` around a scan.
_exclusions: list[tuple[Path, list[str]]] = []


def parse_ignore_lines(text: str) -> list[str]:
    """Patterns from a .depatlasignore file: one per line, # comments, blanks skipped."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_excludes(root: Path, extra: Iterable[str] = ()) -> list[str]:
    """`.depatlasignore` in `root` (if any) plus the extra patterns."""
    patterns: list[str] = []
    ignore_file = Path(root) / ".depatlasignore"
    try:
        if ignore_file.is_file():
            patterns.extend(parse_ignore_lines(ignore_file.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        pass
    patterns.extend(p.strip() for p in extra if p and p.strip())
    return list(dict.fromkeys(patterns))


@contextmanager
def excluding(root: Path, patterns: Iterable[str]):
    """While active, iter_files skips files under `root` that match a pattern."""
    globs = [g for p in patterns for g in path_globs(p)]
    if not globs:
        yield
        return
    entry = (Path(root).resolve(), globs)
    _exclusions.append(entry)
    try:
        yield
    finally:
        _exclusions.remove(entry)


def is_excluded(path: Path) -> bool:
    """True when `path` matches an active exclusion."""
    if not _exclusions:
        return False
    resolved = Path(path).resolve()
    for root, globs in _exclusions:
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(fnmatch.fnmatchcase(rel, g) for g in globs):
            return True
    return False


def iter_files(root: Path, pattern: str = "*") -> Iterator[Path]:
    """Like root.rglob(pattern) for files, but never descends into SKIP_DIRS,
    skips excluded paths, and skips files over MAX_SOURCE_BYTES (with a warning).
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not is_excluded(Path(dirpath) / d)
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if is_excluded(path):
                continue
            # os.walk lists a broken symlink as a file; opening it would fail.
            if not (fnmatch.fnmatch(name, pattern) and path.is_file()):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_SOURCE_BYTES:
                # Only source files are worth a warning; a big data file in a
                # repo (a .csv.bz2 test asset, say) is skipped quietly.
                if language_of(path) and str(path) not in _warned_large:
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


# Top-level folders that hold documentation, samples and tooling, never the
# service's own code or public interface (found by scanning real projects:
# FastAPI's docs_src/, Flask's examples/, scripts/, benchmarks/ ...). Only the
# first path component counts, so `mypkg/scripts/cli.py` is untouched.
AUXILIARY_TOP_DIRS = frozenset(
    {
        "docs", "doc", "docs_src", "examples", "example", "samples", "sample",
        "demo", "demos", "scripts", "benchmarks", "benchmark", "bench",
    }
)


def is_test_path(rel: Path) -> bool:
    """True for files that are not the service's own production code: tests,
    fixtures, and top-level docs/examples/scripts/benchmarks folders.
    Deleting or renaming one of them must never look like a breaking API change.
    The rules are per language, so a production package that merely happens
    to be called `test` (com/org/test/) is not mistaken for tests.
    """
    rel = Path(rel)
    parts = rel.parts
    name = rel.name
    lang = language_of(rel)
    stem = rel.stem
    if len(parts) > 1 and parts[0].lower() in AUXILIARY_TOP_DIRS:
        return True
    # Folders that only ever hold tests or test data. For Java they count only
    # when they come before `src/`, so a production package called `tests`
    # (src/main/java/com/acme/tests/) is not mistaken for tests, while a sample
    # service kept under tests/fixtures/ (with its own src/main/) is.
    test_dirs = {"tests", "fixtures", "testdata", "__fixtures__", "__tests__", "__mocks__"}
    if lang == "java":
        before_src = parts[: parts.index("src")] if "src" in parts else parts[:-1]
        return (
            ("src" in parts and "test" in parts[parts.index("src") + 1 : parts.index("src") + 2])
            or bool(test_dirs & set(before_src))
            or stem.endswith(("Test", "Tests", "IT"))
            or stem.startswith("Test")
        )
    if lang == "python":
        return (
            bool({"tests", "test", "testdata", "fixtures"} & set(parts[:-1]))
            or name.startswith("test_")
            or stem.endswith("_test")
            or name == "conftest.py"
        )
    if lang == "javascript":
        return bool(({"test"} | test_dirs) & set(parts[:-1])) or any(
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
