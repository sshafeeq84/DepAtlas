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
