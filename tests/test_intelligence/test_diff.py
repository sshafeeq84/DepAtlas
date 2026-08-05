from depatlas.intelligence.diff import compute_diff


def _edge(source, target, edge_type="import", source_file="charge.py", line=1, confidence="high"):
    return {
        "source_service": source,
        "target_service": target,
        "type": edge_type,
        "source_file": source_file,
        "source_line": line,
        "confidence": confidence,
    }


def test_new_edge_shows_up_as_added():
    old_edges = []
    new_edges = [_edge("payments-service", "auth-service")]

    diff = compute_diff(old_edges, new_edges)

    assert len(diff["added"]) == 1
    assert diff["added"][0]["target_service"] == "auth-service"
    assert diff["removed"] == []


def test_removed_edge_shows_up_as_removed():
    old_edges = [_edge("payments-service", "auth-service")]
    new_edges = []

    diff = compute_diff(old_edges, new_edges)

    assert diff["added"] == []
    assert len(diff["removed"]) == 1
    assert diff["removed"][0]["target_service"] == "auth-service"


def test_unchanged_edge_shows_up_in_neither():
    edge = _edge("payments-service", "auth-service")

    diff = compute_diff([edge], [edge])

    assert diff["added"] == []
    assert diff["removed"] == []


def test_confidence_only_change_is_not_reported_as_added_or_removed():
    # Same source/target/type/file/line, but confidence differs -- per
    # the module's documented scope, this is NOT a dependency
    # appearing/disappearing, so it should show up in neither list.
    old_edge = _edge("payments-service", "auth-service", confidence="low")
    new_edge = _edge("payments-service", "auth-service", confidence="high")

    diff = compute_diff([old_edge], [new_edge])

    assert diff["added"] == []
    assert diff["removed"] == []


def test_mixed_diff_reports_both_added_and_removed():
    old_edges = [_edge("payments-service", "auth-service")]
    new_edges = [_edge("payments-service", "shipping-service")]

    diff = compute_diff(old_edges, new_edges)

    assert len(diff["added"]) == 1
    assert diff["added"][0]["target_service"] == "shipping-service"
    assert len(diff["removed"]) == 1
    assert diff["removed"][0]["target_service"] == "auth-service"


def test_same_dependency_different_line_counts_as_both_added_and_removed():
    # If a line moves (e.g. code got reformatted), the old identity
    # disappears and a new one appears -- this is a known, accepted
    # limitation of line-based identity rather than a bug: tracking
    # "the same logical edge moved" would need more than line-number
    # matching (e.g. AST-level move detection), which is out of scope.
    old_edge = _edge("payments-service", "auth-service", line=10)
    new_edge = _edge("payments-service", "auth-service", line=12)

    diff = compute_diff([old_edge], [new_edge])

    assert len(diff["added"]) == 1
    assert len(diff["removed"]) == 1
