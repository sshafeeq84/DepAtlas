"""The scan pipeline as a function, shared by `scan` and `scan-all`:
parse a repo's code, build the graph nodes, persist edges and a snapshot in
one transaction, and (optionally) run the per-scan alerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from depatlas.fsutil import detect_languages, is_test_path
from depatlas.gitinfo import get_sha
from depatlas.graph.builder import build_graph
from depatlas.graph.store import GraphStore
from depatlas.intelligence.alerts import (
    Alert,
    check_breaking_changes,
    check_high_fan_in,
    check_new_cross_team_dependencies,
    check_orphaned_service,
    check_unowned_service,
    comparable_signatures,
)
from depatlas.intelligence.diff import compute_diff
from depatlas.ownership.codeowners import parse_codeowners


@dataclass
class ScanResult:
    self_service: str
    languages: list[str]
    edges: list
    nodes: list
    snapshot_id: int
    alerts: list[Alert] = field(default_factory=list)


def resolve_languages(
    repo_path: Path, requested: tuple[str, ...] | list[str], parsers: dict, log: Callable[[str], None]
) -> list[str]:
    """The languages to parse: what was asked for, or detected from files."""
    if not requested:
        detected = detect_languages(repo_path) or ["python"]
        log(f"Detected language(s): {', '.join(detected)}")
        return detected
    if "all" in requested:
        return sorted(parsers)
    return list(dict.fromkeys(requested))


def run_scan(
    repo_path: Path,
    repo_key: str,
    known_services: dict[str, str],
    known_endpoints: dict[str, str],
    languages: list[str],
    parsers: dict,
    extractors: dict,
    db_path: Path,
    codeowners_path: Path | None = None,
    manifest_teams: dict[str, str] | None = None,
    ownership_inputs_supplied: bool = False,
    alerts_enabled: bool = False,
    fan_in_threshold: int = 10,
    log: Callable[[str], None] = lambda message: None,
) -> ScanResult:
    self_service = known_services.get("__self__", "unknown-service")
    codeowners_rules = parse_codeowners(codeowners_path) if codeowners_path else None

    edges = []
    for lang in languages:
        edges.extend(parsers[lang](repo_path, known_services, known_endpoints))
    commit_sha = get_sha(repo_path)
    for edge in edges:
        edge.detected_at_sha = commit_sha
        edge.from_test = is_test_path(Path(edge.source_file))
    nodes = build_graph(
        edges,
        repo_path,
        self_service,
        language=",".join(languages),
        codeowners_rules=codeowners_rules,
        manifest_teams=manifest_teams,
        repo_id=repo_key,
    )
    for node in nodes:
        if node.id == self_service:
            node.last_analysed_sha = commit_sha

    signatures = []
    for lang in languages:
        signatures.extend(extractors[lang](repo_path, self_service))

    store = GraphStore(db_path)
    try:
        with store.transaction():  # services, edges and snapshot commit together
            store.upsert_services(nodes, ownership_inputs_supplied=ownership_inputs_supplied)
            store.replace_edges_for_repo(repo_key, edges, self_service=self_service)
            snapshot_id = store.save_snapshot(
                repo_key, edges, signatures=signatures, languages=languages
            )

        triggered: list[Alert] = []
        if alerts_enabled:
            service_teams = {s["id"]: s["team"] for s in store.all_services()}

            # NEW_CROSS_TEAM_DEPENDENCY and BREAKING_CHANGE both need a diff
            # -- only possible once this repo has at least two snapshots.
            recent = store.list_snapshots(repo=repo_key, limit=2)
            if len(recent) == 2:
                old_snapshot = store.get_snapshot(recent[1]["id"])
                new_snapshot = store.get_snapshot(recent[0]["id"])
                diff_result = compute_diff(
                    old_snapshot["edges"], new_snapshot["edges"], by_location=False
                )
                triggered += check_new_cross_team_dependencies(diff_result["added"], service_teams)

                old_sigs, new_sigs, skipped = comparable_signatures(old_snapshot, new_snapshot)
                if skipped:
                    log(
                        "warning: language set changed since the previous scan; "
                        f"not comparing signatures for: {', '.join(skipped)}"
                    )
                triggered += check_breaking_changes(
                    self_service,
                    old_sigs,
                    new_sigs,
                    store.edges_for_service(self_service, direction="upstream"),
                )

            # The other three are point-in-time checks against the service
            # just scanned.
            downstream = store.edges_for_service(self_service, direction="downstream")
            upstream = store.edges_for_service(self_service, direction="upstream")
            triggered += check_orphaned_service(self_service, downstream, upstream)
            triggered += check_unowned_service(self_service, service_teams.get(self_service))
            triggered += check_high_fan_in(self_service, upstream, threshold=fan_in_threshold)
    finally:
        store.close()

    return ScanResult(self_service, languages, edges, nodes, snapshot_id, triggered)
