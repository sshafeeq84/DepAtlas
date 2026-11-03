"""Commit-SHA bookkeeping, so each scan can record *which commit* it saw."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _sidecar(repo_dir: Path) -> Path:
    return repo_dir.parent / f"{repo_dir.name}.sha"


def write_sha(repo_dir: Path, sha: str | None) -> None:
    """Remember the commit a connector fetched into `repo_dir` (stored next
    to the folder, not inside it, so it is never scanned as source)."""
    if sha:
        _sidecar(repo_dir).write_text(sha, encoding="utf-8")


def clear_sha(repo_dir: Path) -> None:
    """Forget any SHA recorded by an earlier fetch into `repo_dir`, so a
    fetch that records none can never inherit the previous scan's."""
    _sidecar(repo_dir).unlink(missing_ok=True)


def get_sha(repo_dir: Path) -> str | None:
    """The commit SHA for a scanned repo: the one a connector recorded, else
    `git rev-parse HEAD` for a local checkout, else None."""
    sidecar = _sidecar(repo_dir)
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip() or None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None
