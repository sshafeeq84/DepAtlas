"""Graph builder (SPEC.md section 5, "Graph Builder" box).

Takes the flat DependencyEdge stream a parser produces and resolves it
into ServiceNode objects — the "service identity resolution" step the
spec calls out. For v1, identity resolution is intentionally simple:

  - The service actually being scanned (source_service, named via the
    manifest's "__self__" key) gets a full node: we know its repo path
    and language.
  - Any *other* service name that shows up as a target of an edge (e.g.
    "auth-service", discovered because payments-service imports it) gets
    a placeholder node: we know it exists and is depended upon, but not
    its repo or language, until its own repo gets scanned directly.

This placeholder approach is what makes multi-repo accumulation work:
scanning payments-service today creates a stub node for auth-service;
scanning auth-service's own repo tomorrow fills that stub in with real
data via the same service name, rather than creating a duplicate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from depgraph.graph.models import DependencyEdge, ServiceNode


def build_graph(
    edges: list[DependencyEdge],
    repo_path: Path,
    self_service: str,
    language: str = "python",
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
    nodes: list[ServiceNode] = []

    for name in sorted(known_service_names):
        if name == self_service:
            nodes.append(
                ServiceNode(
                    id=name,
                    name=name,
                    repo=str(repo_path),
                    language=language,
                    last_analysed_at=now,
                )
            )
        else:
            # A dependency we know about only because something points at
            # it — not yet scanned directly, so repo/language are unknown.
            nodes.append(
                ServiceNode(
                    id=name,
                    name=name,
                    repo="",
                    language="",
                )
            )

    return nodes
