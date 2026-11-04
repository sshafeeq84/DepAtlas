"""Stateless pull-request check: `depatlas check --base origin/main`.

`scan` + `alerts` need a graph database that persists between runs, which a
fresh CI runner does not have. This module does the same comparison in one
run, with no database:

  1. find the merge-base of the base ref and HEAD;
  2. extract that commit to a temporary folder (`git archive`, so the
     checkout is never touched);
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

import io
import json
import subprocess
import tarfile
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


def _git(repo: Path, *args: str, binary: bool = False):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=not binary,
            timeout=120,
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
    """Extract `commit`'s version of `repo` (a folder inside a git checkout,
    possibly a subfolder of a monorepo) into `dest`. Returns False when the
    folder did not exist at that commit (a brand-new service).
    """
    # Run from inside the folder, `git archive` already limits itself to that
    # folder and names files relative to it. It returns an empty archive
    # (not an error) for a folder that did not exist yet, so check first.
    prefix = _git(repo, "rev-parse", "--show-prefix").stdout.strip().rstrip("/")
    if prefix and _git(repo, "cat-file", "-e", f"{commit}:{prefix}").returncode != 0:
        return False
    result = _git(repo, "archive", "--format=tar", commit, binary=True)
    if result.returncode != 0:
        raise CheckError(f"git archive failed: {result.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # older Pythons: refuse anything that escapes `dest`
            root = dest.resolve()
            for member in tar.getmembers():
                if not (dest / member.name).resolve().is_relative_to(root):
                    raise CheckError(f"Unsafe path in archive: {member.name}")
            tar.extractall(dest)
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

        if languages:
            used = list(languages)
        else:
            used = sorted(set(detect_languages(repo_path)) | set(detect_languages(base_dir))) or [
                "python"
            ]

        new_edges, new_sigs = analyse(
            repo_path, used, known_services, known_endpoints, self_service, parsers, extractors
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

    diff = compute_diff(
        [e.to_dict() for e in old_edges], [e.to_dict() for e in new_edges], by_location=False
    )
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
