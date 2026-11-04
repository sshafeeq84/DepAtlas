"""Stateless pull-request check: `depatlas check --base origin/main`.

`scan` + `alerts` need a graph database that persists between runs, which a
fresh CI runner does not have. This module does the same comparison in one
run, with no database:

  1. find the merge-base of the base ref and HEAD;
  2. extract that commit to a temporary folder (from a temporary git
     index, so the checkout is never touched);
  3. analyse the base tree and the working tree the same way `scan` does
     (edges and public signatures);
  4. report the alerts that need a before/after comparison:
       - NEW_CROSS_TEAM_DEPENDENCY (edges added since the base)
       - BREAKING_CHANGE (public functions removed or changed, when other
         services depend on this one)
  5. read teams and dependents from an org-wide graph export
     (`depatlas export --format json`, produced by a nightly job that scans
     every repo), because one repo cannot know who depends on it.

The point-in-time alerts (HIGH_FAN_IN, ORPHANED_SERVICE, UNOWNED_SERVICE) are
about the whole graph, not about a change, so they stay in `depatlas alerts`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from depatlas.fsutil import detect_languages, is_test_path
from depatlas.graph.builder import build_graph
from depatlas.intelligence.alerts import (
    Alert,
    check_breaking_changes,
    check_new_cross_team_dependencies,
)
from depatlas.intelligence.diff import compute_diff


class CheckError(Exception):
    """A problem the user can fix (bad ref, shallow clone, bad graph file)."""


def _git(repo: Path, *args: str, binary: bool = False, env: dict | None = None):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=not binary,
            timeout=120,
            env={**os.environ, **env} if env else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckError(f"Could not run git: {exc}") from exc
    return result


def resolve_merge_base(repo: Path, base: str) -> str:
    """The commit where HEAD branched from `base`."""
    result = _git(repo, "merge-base", base, "HEAD")
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise CheckError(
            f"Could not find a merge-base between '{base}' and HEAD. "
            "Check that the ref exists. On a CI runner the checkout must "
            "include history: in GitHub Actions use "
            "`actions/checkout` with `fetch-depth: 0`."
        )
    return sha


def export_tree(repo: Path, commit: str, dest: Path) -> bool:
    """Write `commit`'s version of `repo` (a folder inside a git checkout,
    possibly a subfolder of a monorepo) into `dest`. Returns False when the
    folder did not exist at that commit (a brand-new service).

    The files come from a temporary index (`read-tree` + `checkout-index`),
    so the real checkout and index are never touched, `export-ignore`
    attributes do not hide files, and git itself refuses paths that would
    land outside `dest`. Git-ignored and generated files are not part of a
    commit, so they never appear.
    """
    prefix = _git(repo, "rev-parse", "--show-prefix").stdout.strip().rstrip("/")
    if prefix and _git(repo, "cat-file", "-e", f"{commit}:{prefix}").returncode != 0:
        return False
    tree = f"{commit}:{prefix}" if prefix else commit
    # Run from the top of the checkout: from a subfolder, checkout-index -a
    # would only write entries under that subfolder's path.
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip() or str(repo)
    with tempfile.TemporaryDirectory(prefix="depatlas-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        read = _git(top, "read-tree", tree, env=env)
        if read.returncode != 0:
            raise CheckError(f"git read-tree failed: {read.stderr.strip()}")
        out = _git(top, "checkout-index", "-a", f"--prefix={dest.resolve().as_posix()}/", env=env)
        if out.returncode != 0:
            raise CheckError(f"git checkout-index failed: {out.stderr.strip()}")
    return True


def load_graph(path: Path) -> tuple[list[dict], list[dict]]:
    """Nodes and edges from `depatlas export --format json`."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        nodes, edges = data["nodes"], data["edges"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckError(
            f"{path} is not a DepAtlas graph export (expected the output of "
            f"`depatlas export --format json`): {exc}"
        ) from exc
    return nodes, edges


def analyse(
    repo_path: Path,
    languages: list[str],
    known_services: dict,
    known_endpoints: dict,
    self_service: str,
    parsers: dict[str, Callable],
    extractors: dict[str, Callable],
) -> tuple[list, list]:
    """Edges (tagged `from_test`) and public signatures for one tree."""
    edges = []
    signatures = []
    for lang in languages:
        edges.extend(parsers[lang](repo_path, known_services, known_endpoints))
        signatures.extend(extractors[lang](repo_path, self_service))
    for edge in edges:
        edge.from_test = is_test_path(Path(edge.source_file))
    return edges, signatures


def run_check(
    repo_path: Path,
    base: str,
    known_services: dict,
    known_endpoints: dict,
    languages: list[str] | None,
    parsers: dict[str, Callable],
    extractors: dict[str, Callable],
    graph_path: Path | None = None,
    codeowners_rules=None,
    manifest_teams: dict | None = None,
    use_working_tree: bool = False,
) -> dict:
    """Run the check. Returns {"alerts", "base_sha", "notes", "languages"}."""
    self_service = known_services.get("__self__", "unknown-service")
    notes: list[str] = []

    base_sha = resolve_merge_base(repo_path, base)

    graph_nodes: list[dict] = []
    graph_edges: list[dict] = []
    if graph_path is not None:
        graph_nodes, graph_edges = load_graph(graph_path)
    else:
        notes.append(
            "No --graph given: teams and dependents are unknown, so "
            "NEW_CROSS_TEAM_DEPENDENCY and BREAKING_CHANGE cannot fire."
        )

    with tempfile.TemporaryDirectory(prefix="depatlas-base-") as tmp:
        base_dir = Path(tmp) / "base"
        base_dir.mkdir()
        base_existed = export_tree(repo_path, base_sha, base_dir)
        if not base_existed:
            notes.append(f"{repo_path} does not exist at {base_sha[:8]}: treating it as new.")

        # Both sides come out of git the same way (from git), so files
        # that are ignored, generated by an earlier CI step, export-ignored
        # or LFS pointers look the same on both. `use_working_tree` compares
        # the files on disk instead, to include uncommitted edits.
        if use_working_tree:
            head_dir = repo_path
        else:
            head_dir = Path(tmp) / "head"
            head_dir.mkdir()
            if not export_tree(repo_path, "HEAD", head_dir):
                notes.append(
                    f"{repo_path} is not committed at HEAD, so there is nothing to check; "
                    "commit it, or pass --working-tree."
                )
            elif _git(repo_path, "status", "--porcelain", "--", ".").stdout.strip():
                notes.append(
                    "uncommitted changes are not checked (comparing HEAD with the base); "
                    "commit them, or pass --working-tree."
                )

        if languages:
            used = list(languages)
        else:
            used = sorted(set(detect_languages(head_dir)) | set(detect_languages(base_dir))) or [
                "python"
            ]

        new_edges, new_sigs = analyse(
            head_dir, used, known_services, known_endpoints, self_service, parsers, extractors
        )
        old_edges, old_sigs = (
            analyse(base_dir, used, known_services, known_endpoints, self_service, parsers, extractors)
            if base_existed
            else ([], [])
        )

    # Teams: the org graph, overlaid with this repo's own ownership inputs.
    service_teams = {n["id"]: n.get("team") for n in graph_nodes}
    if codeowners_rules or manifest_teams:
        own = build_graph(
            new_edges,
            repo_path,
            self_service,
            language=",".join(used),
            codeowners_rules=codeowners_rules,
            manifest_teams=manifest_teams,
        )
        for node in own:
            if node.id == self_service and node.team:
                service_teams[self_service] = node.team

    old_edge_dicts = [e.to_dict() for e in old_edges]
    if not base_existed:
        # A new or moved folder has no base to compare with, so every
        # dependency would look new. The org graph already knows what this
        # service depended on; use that as the baseline (a genuinely new
        # service has none, and is still all-new).
        old_edge_dicts = [e for e in graph_edges if e.get("source_service") == self_service]
        if old_edge_dicts:
            notes.append(
                f"using the {len(old_edge_dicts)} dependencies the org graph already has for "
                f"'{self_service}' as the baseline."
            )
    diff = compute_diff(old_edge_dicts, [e.to_dict() for e in new_edges], by_location=False)
    alerts: list[Alert] = []
    alerts += check_new_cross_team_dependencies(diff["added"], service_teams)

    # Dependents come from other repos' scans; this repo's own edges are
    # replaced by the fresh scan, so they are not counted.
    dependents = [
        e
        for e in graph_edges
        if e.get("target_service") == self_service and e.get("source_service") != self_service
    ]
    alerts += check_breaking_changes(
        self_service,
        [s.to_dict() for s in old_sigs],
        [s.to_dict() for s in new_sigs],
        dependents,
    )

    return {"alerts": alerts, "base_sha": base_sha, "notes": notes, "languages": used}
