"""Serialize DependencyEdges to JSON (SPEC.md section 4.1, Graph output)."""

from __future__ import annotations

import json
from pathlib import Path

from depgraph.graph.models import DependencyEdge


def export_edges(edges: list[DependencyEdge], output_path: Path | None = None) -> str:
    payload = {"edges": [edge.to_dict() for edge in edges]}
    text = json.dumps(payload, indent=2)

    if output_path is not None:
        output_path.write_text(text, encoding="utf-8")

    return text


def export_graph_json(
    nodes: list[dict], edges: list[dict], output_path: Path | None = None
) -> str:
    """Export the whole persisted graph (as returned by
    GraphStore.all_services() / all_edges(), already plain dicts -- not
    DependencyEdge/ServiceNode objects) to JSON. Used by
    `depgraph export --format json`, as opposed to `export_edges`, which
    is what a single `scan`'s `--output` writes.
    """
    payload = {"nodes": nodes, "edges": edges}
    text = json.dumps(payload, indent=2)

    if output_path is not None:
        output_path.write_text(text, encoding="utf-8")

    return text
