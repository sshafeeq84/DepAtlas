from depgraph.intelligence.alerts import (
    check_breaking_changes,
    check_high_fan_in,
    check_new_cross_team_dependencies,
    check_orphaned_service,
    check_unowned_service,
)


def _edge(source, target):
    return {"source_service": source, "target_service": target}


# --- NEW_CROSS_TEAM_DEPENDENCY ---


def test_flags_new_edge_crossing_team_boundary():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team", "auth-service": "platform-team"}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert len(alerts) == 1
    assert alerts[0].type == "NEW_CROSS_TEAM_DEPENDENCY"
    assert alerts[0].severity == "medium"


def test_does_not_flag_same_team_dependency():
    added = [_edge("payments-service", "billing-service")]
    teams = {"payments-service": "payments-team", "billing-service": "payments-team"}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


def test_does_not_flag_when_either_service_unowned():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team", "auth-service": None}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


def test_does_not_flag_when_team_unknown_entirely():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team"}  # auth-service not in the map at all

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


# --- HIGH_FAN_IN ---


def test_flags_high_fan_in_at_threshold():
    upstream = [_edge(f"service-{i}", "auth-service") for i in range(10)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=10)

    assert len(alerts) == 1
    assert alerts[0].type == "HIGH_FAN_IN"
    assert alerts[0].severity == "medium"


def test_does_not_flag_below_threshold():
    upstream = [_edge(f"service-{i}", "auth-service") for i in range(9)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=10)

    assert alerts == []


def test_counts_distinct_dependents_not_raw_edges():
    # Same dependent service, 3 separate import edges -- should count as 1.
    upstream = [_edge("payments-service", "auth-service") for _ in range(3)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=2)

    assert alerts == []


# --- ORPHANED_SERVICE ---


def test_flags_service_with_no_edges_at_all():
    alerts = check_orphaned_service("leaf-service", downstream_edges=[], upstream_edges=[])

    assert len(alerts) == 1
    assert alerts[0].type == "ORPHANED_SERVICE"
    assert alerts[0].severity == "low"


def test_does_not_flag_service_with_only_downstream_edges():
    alerts = check_orphaned_service(
        "payments-service",
        downstream_edges=[_edge("payments-service", "auth-service")],
        upstream_edges=[],
    )

    assert alerts == []


def test_does_not_flag_service_with_only_upstream_edges():
    alerts = check_orphaned_service(
        "auth-service",
        downstream_edges=[],
        upstream_edges=[_edge("payments-service", "auth-service")],
    )

    assert alerts == []


# --- UNOWNED_SERVICE ---


def test_flags_service_with_no_team():
    alerts = check_unowned_service("payments-service", team=None)

    assert len(alerts) == 1
    assert alerts[0].type == "UNOWNED_SERVICE"
    assert alerts[0].severity == "low"


def test_does_not_flag_service_with_a_team():
    alerts = check_unowned_service("payments-service", team="payments-team")

    assert alerts == []


# --- BREAKING_CHANGE ---


def _sig(name, params):
    """params: list of (name, type) tuples for brevity in tests --
    converted to the real {"name", "type"} dict shape here."""
    return {"name": name, "params": [{"name": n, "type": t} for n, t in params]}


def test_flags_removed_public_function_when_service_has_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = []
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert alerts[0].type == "BREAKING_CHANGE"
    assert alerts[0].severity == "high"


def test_flags_parameter_count_change_when_service_has_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "int"), ("currency", "str")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert "createCharge" in alerts[0].message


def test_flags_type_change_at_same_position_when_both_sides_typed():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "str")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert "int" in alerts[0].message and "str" in alerts[0].message


def test_does_not_flag_pure_parameter_rename():
    # Same position, same type, only the name differs -- most real
    # callers invoke positionally, so this is deliberately NOT breaking.
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("total", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_type_change_when_one_side_has_no_type_info():
    # Only the new side is typed -- there's nothing concrete to compare
    # against on the old side, so this isn't flagged as a type change
    # (gaining an annotation isn't itself a behavior change).
    old_sigs = [_sig("createCharge", [("amount", None)])]
    new_sigs = [_sig("createCharge", [("amount", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_when_service_has_no_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = []  # removed, but nobody depends on this service

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream_edges=[])

    assert alerts == []


def test_does_not_flag_unchanged_signature():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_newly_added_function():
    # Adding a new public function isn't breaking -- only removing or
    # changing an existing one is.
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [
        _sig("createCharge", [("amount", "int")]),
        _sig("refundCharge", [("chargeId", "str")]),
    ]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_multiple_breaking_changes_all_reported():
    old_sigs = [
        _sig("createCharge", [("amount", "int")]),
        _sig("refundCharge", [("chargeId", "str")]),
    ]
    new_sigs = [
        _sig("createCharge", [("amount", "int"), ("currency", "str")])
    ]  # refundCharge removed, createCharge's param count changed
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 2
