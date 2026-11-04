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


def _edge_identity(edge: dict, by_location: bool = True) -> tuple:
    """The identity of an edge for diffing purposes: which service
    depends on which, how, and where in the source it was found.
    Confidence is deliberately excluded -- see module docstring.
    """
    if not by_location:
        # "Does A depend on B (this way)?" -- unaffected by where in the
        # code the call lives, so reformatting or moving code is not a
        # new dependency.
        return (edge["source_service"], edge["target_service"], edge["type"])
    return (
        edge["source_service"],
        edge["target_service"],
        edge["type"],
        edge["source_file"],
        edge["source_line"],
    )


def _index(edges: list[dict], by_location: bool) -> dict:
    """Edges keyed by identity. When several edges share an identity (the
    same dependency found in a production file and in a test file), keep a
    non-test one, so the dependency is not discounted as test-only.
    """
    indexed: dict = {}
    for edge in edges:
        key = _edge_identity(edge, by_location)
        if key not in indexed or (indexed[key].get("from_test") and not edge.get("from_test")):
            indexed[key] = edge
    return indexed


def compute_diff(old_edges: list[dict], new_edges: list[dict], by_location: bool = True) -> dict:
    """Compare two edge lists (as produced by GraphStore.get_snapshot) and
    return {"added": [...], "removed": [...]}, each a list of edge dicts.

    By default an edge's identity includes file and line, so the `diff`
    command shows every change in *where* a dependency lives. Pass
    by_location=False (as alerting does) to compare only source, target
    and type, so moved or reformatted code does not look like a new
    dependency.
    """
    old_by_identity = _index(old_edges, by_location)
    new_by_identity = _index(new_edges, by_location)

    added = [e for key, e in new_by_identity.items() if key not in old_by_identity]
    removed = [e for key, e in old_by_identity.items() if key not in new_by_identity]

    return {"added": added, "removed": removed}
