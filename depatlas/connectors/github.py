"""GitHub connector (SPEC.md section 4.1, "Code store connectors": GitHub).

Downloads a repository's contents via the GitHub REST API's zipball
endpoint and extracts them into a local cache directory, so the existing
language parsers can run against the result unchanged -- this connector's
only job is "get the code onto disk," matching the "Repository Fetcher
(clone / stream / cache layer)" box in SPEC.md's architecture diagram.

Deliberately uses the REST API + zip download rather than shelling out to
`git clone`, so this project has no dependency on git being installed or
on PATH -- the same "keep runtime dependencies minimal" principle behind
choosing esprima over a Node-based JS parser.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import requests

GITHUB_API_BASE = "https://api.github.com"
TOKEN_ENV_VAR = "DEPATLAS_GITHUB_TOKEN"


class GitHubConnectorError(Exception):
    """Raised when the GitHub API can't be reached or returns an error."""


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_token() -> str:
    """Read the GitHub token from the environment (SPEC.md section 9's
    config convention: token_env: DEPATLAS_GITHUB_TOKEN). Never accepts
    a token as a CLI argument or writes one to disk (SPEC.md section 12:
    "Auth credential storage: Environment variables only -- never
    written to disk").
    """
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise GitHubConnectorError(
            f"No GitHub token found. Set the {TOKEN_ENV_VAR} environment variable."
        )
    return token


def fetch_repo(owner: str, repo: str, token: str, cache_dir: Path, ref: str = "HEAD") -> Path:
    """Download `owner/repo` at `ref` (default: the repo's default branch)
    as a zip via the GitHub API, extract it under cache_dir, and return
    the path to the extracted repo's root directory.

    Re-downloads and overwrites the cached copy on every call -- there's
    no partial-update/diff logic here, matching the "one-time setup,
    always current" spirit from SPEC.md's vision: each scan should
    reflect the current remote state, not a possibly-stale local cache.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/zipball/{ref}"

    try:
        response = requests.get(url, headers=_auth_headers(token), timeout=30)
    except requests.RequestException as exc:
        raise GitHubConnectorError(f"Failed to reach GitHub API: {exc}") from exc

    if response.status_code == 404:
        raise GitHubConnectorError(f"Repo '{owner}/{repo}' not found (or token lacks access).")
    if response.status_code == 401:
        raise GitHubConnectorError("GitHub token is invalid or expired.")
    if response.status_code != 200:
        raise GitHubConnectorError(
            f"GitHub API returned {response.status_code} for {owner}/{repo}: {response.text[:200]}"
        )

    extract_root = Path(cache_dir) / f"{owner}__{repo}"
    extract_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        zf.extractall(extract_root)

    # GitHub's zipball wraps everything in a single top-level folder
    # named like "owner-repo-<short-sha>" -- resolve down into it so
    # callers get the actual repo root, not the wrapper folder.
    inner_dirs = [p for p in extract_root.iterdir() if p.is_dir()]
    if len(inner_dirs) == 1:
        return inner_dirs[0]
    return extract_root
