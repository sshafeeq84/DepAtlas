"""DOT (Graphviz) export (SPEC.pdf section 4.1, "Graph output": JSON, DOT
format; and section 9's `depatlas export --format dot`).

Produces a directed graph in Graphviz's DOT language: one node per
service (labeled with its team, if known) and one edge per dependency
(labeled with its type and confidence). This is deliberately just text
generation -- no graphviz library dependency -- since DOT is a simple
enough format to emit directly, and it keeps this project's dependency
list from growing for a feature that's really just string formatting.
"""

from __future__ import annotations


def _escape(value: str) -> str:
    """Escape double quotes for safe use inside a DOT quoted string."""
    return value.replace('"', '\\"')


def export_dot(nodes: list[dict], edges: list[dict]) -> str:
    """Render a list of service dicts (as returned by
    GraphStore.all_services()) and edge dicts (as returned by
    GraphStore.all_edges()) as a Graphviz DOT digraph.
    """
    lines = ["digraph depatlas {"]

    for node in nodes:
        label = _escape(node["id"])
        if node.get("team"):
            label += f"\\n({_escape(node['team'])})"
        lines.append(f'  "{_escape(node["id"])}" [label="{label}"];')

    for edge in edges:
        label = f"{edge['type']} ({edge['confidence']})"
        lines.append(
            f'  "{_escape(edge["source_service"])}" -> '
            f'"{_escape(edge["target_service"])}" [label="{_escape(label)}"];'
        )

    lines.append("}")
    return "\n".join(lines)
