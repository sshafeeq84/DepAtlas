"""CODEOWNERS ownership mapping (SPEC.pdf section 8, priority 1).

GitHub's CODEOWNERS format is a list of lines, each either a comment
(starting with `#`), blank, or a rule: a gitignore-style path pattern
followed by one or more owner handles, e.g.:

    # payments-service ownership
    *.py            @payments-team
    /tests/          @qa-team

The real CODEOWNERS matching rule (and the one we replicate here) is:
rules are evaluated top to bottom, and the *last* matching rule wins —
not the first, and not the most specific. This mirrors git's own
behavior, so a hand-written CODEOWNERS file behaves the same way here
as it would on GitHub itself.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OwnershipRule:
    pattern: str
    owner: str  # first owner handle if multiple are listed on one line


def parse_codeowners(path: Path) -> list[OwnershipRule]:
    """Parse a CODEOWNERS file into an ordered list of rules."""
    rules: list[OwnershipRule] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue  # malformed line (pattern with no owner); skip it

        pattern, *owners = parts
        rules.append(OwnershipRule(pattern=pattern, owner=owners[0]))

    return rules


def _globs(pattern: str) -> list[str]:
    """The fnmatch globs that together express one CODEOWNERS pattern.

    gitignore-style rules: a pattern with a "/" at the start or in the
    middle is anchored to the repo root, any other matches at any depth; a
    trailing "/" means everything inside that folder; a bare name matches a
    file of that name or a folder of that name (and what is inside it).
    Negation and "**" are out of scope. fnmatch's "*" also crosses "/",
    which is more generous than gitignore for patterns like `docs/*`.
    """
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.lstrip("/")
    if body.endswith("/"):
        body += "*"
    globs = [body] if "*" in body else [body, body + "/*"]
    return globs if anchored else globs + ["*/" + g for g in globs]


def resolve_owner(file_path: str, repo_root: Path, rules: list[OwnershipRule]) -> str | None:
    """Resolve which team/owner is responsible for a given file.

    file_path may be absolute or relative; it's normalized to be
    relative to repo_root (with forward slashes, regardless of OS)
    before matching, since CODEOWNERS patterns are always repo-relative.
    """
    try:
        rel_path = Path(file_path).resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel_path = Path(file_path)

    rel_str = rel_path.as_posix()

    owner: str | None = None
    for rule in rules:
        if any(fnmatch.fnmatchcase(rel_str, glob) for glob in _globs(rule.pattern)):
            owner = rule.owner  # last match wins -- keep overwriting

    return owner
