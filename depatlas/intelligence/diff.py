"""Diff engine (SPEC.pdf section 5, Intelligence Layer: "Diff engine:
compares graph versions").

Compares two snapshots' edge sets and reports which dependencies were
added or removed between them. Confidence-level changes on an otherwise
unchanged edge (e.g. a heuristic "low" match later resolving to an
explicit "high" one) are deliberately *not* reported as added/removed --
only genuine appearance/disappearance of a dependency counts. Surfacing
confidence-only changes is a reasonable future enhancement, but it's a
different question ("did our confidence in this edge change?") from what
this v1 diff answers ("did this dependency start or stop existing?").
"""

from __future__ import annotations


def _edge_identity(edge: dict) -> tuple:
    """The identity of an edge for diffing purposes: which service
    depends on which, how, and where in the source it was found.
    Confidence is deliberately excluded -- see module docstring.
    """
    return (
        edge["source_service"],
        edge["target_service"],
        edge["type"],
        edge["source_file"],
        edge["source_line"],
    )


def compute_diff(old_edges: list[dict], new_edges: list[dict]) -> dict:
    """Compare two edge lists (as produced by GraphStore.get_snapshot) and
    return {"added": [...], "removed": [...]}, each a list of edge dicts.
    """
    old_by_identity = {_edge_identity(e): e for e in old_edges}
    new_by_identity = {_edge_identity(e): e for e in new_edges}

    added = [e for key, e in new_by_identity.items() if key not in old_by_identity]
    removed = [e for key, e in old_by_identity.items() if key not in new_by_identity]

    return {"added": added, "removed": removed}
