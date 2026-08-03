from depgraph.intelligence.alerts import (
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
