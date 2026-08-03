"""Graph builder (SPEC.md section 5, "Graph Builder" box).

Takes the flat DependencyEdge stream a parser produces and resolves it
into ServiceNode objects -- the "service identity resolution" and
"maps services to team ownership" steps the spec calls out. For v1,
both are intentionally simple:

  - The service actually being scanned (source_service, named via the
    manifest's "__self__" key) gets a full node: we know its repo path
    and language, and -- if a CODEOWNERS file is supplied -- its team.
  - Any *other* service name that shows up as a target of an edge (e.g.
    "auth-service", discovered because payments-service imports it) gets
    a placeholder node: we know it exists and is depended upon, but not
    its repo, language, or team, until its own repo gets scanned directly.

This placeholder approach is what makes multi-repo accumulation work:
scanning payments-service today creates a stub node for auth-service;
scanning auth-service's own repo tomorrow fills that stub in with real
data via the same service name, rather than creating a duplicate.

Team resolution (SPEC.md section 8) follows the spec's priority order:
  1. CODEOWNERS -- resolving an owner for every file in the repo
     individually (not just one language's files -- ownership isn't
     language-specific), then taking the most common owner as the
     service's team. A service with a few stray files owned by a
     different team still gets attributed to whichever team owns most
     of its code.
  2. depgraph.yaml manifest -- an explicit {service_name: team} lookup,
     used only when CODEOWNERS wasn't supplied or didn't resolve a team.
  3. Unowned -- if neither source names a team, `ServiceNode.team` stays
     None, matching SPEC.md 8's "Fallback -- unowned; flagged in output."
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from depgraph.graph.models import DependencyEdge, ServiceNode
from depgraph.ownership.codeowners import OwnershipRule, resolve_owner


def _resolve_team(
    repo_path: Path,
    self_service: str,
    codeowners_rules: list[OwnershipRule] | None,
    manifest_teams: dict[str, str] | None,
) -> str | None:
    if codeowners_rules:
        owners_found = []
        for file_path in repo_path.rglob("*"):
            if not file_path.is_file():
                continue
            owner = resolve_owner(str(file_path), repo_path, codeowners_rules)
            if owner is not None:
                owners_found.append(owner)

        if owners_found:
            most_common_owner, _count = Counter(owners_found).most_common(1)[0]
            return most_common_owner
        # CODEOWNERS was supplied but resolved nothing for this repo --
        # fall through to the manifest rather than stopping here.

    if manifest_teams and self_service in manifest_teams:
        return manifest_teams[self_service]

    return None  # SPEC.md 8, priority 3: unowned; flagged in output


def build_graph(
    edges: list[DependencyEdge],
    repo_path: Path,
    self_service: str,
    language: str = "python",
    codeowners_rules: list[OwnershipRule] | None = None,
    manifest_teams: dict[str, str] | None = None,
) -> list[ServiceNode]:
    """Resolve a batch of edges (from one repo scan) into ServiceNodes.

    Always returns at least a node for `self_service`, even if the scan
    found zero edges (an empty or leaf service is still a real node).
    """
    known_service_names = {self_service}
    for edge in edges:
        known_service_names.add(edge.source_service)
        known_service_names.add(edge.target_service)

    now = datetime.now(timezone.utc).isoformat()
    team = _resolve_team(repo_path, self_service, codeowners_rules, manifest_teams)
    nodes: list[ServiceNode] = []

    for name in sorted(known_service_names):
        if name == self_service:
            nodes.append(
                ServiceNode(
                    id=name,
                    name=name,
                    repo=str(repo_path),
                    language=language,
                    team=team,
                    last_analysed_at=now,
                )
            )
        else:
            # A dependency we know about only because something points at
            # it -- not yet scanned directly, so repo/language/team are
            # unknown.
            nodes.append(
                ServiceNode(
                    id=name,
                    name=name,
                    repo="",
                    language="",
                )
            )

    return nodes
