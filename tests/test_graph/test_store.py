from pathlib import Path

import pytest

from depgraph.graph.models import DependencyEdge, ServiceNode
from depgraph.graph.store import GraphStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / ".depgraph" / "graph.db"
    s = GraphStore(db_path)
    yield s
    s.close()


def _node(id_, repo="", language=""):
    return ServiceNode(id=id_, name=id_, repo=repo, language=language)


def _edge(source, target, source_file):
    return DependencyEdge(
        source_service=source,
        target_service=target,
        type="import",
        source_file=source_file,
        source_line=1,
        confidence="high",
    )


def test_upsert_and_read_back_services(store):
    store.upsert_services([_node("payments-service", repo="/repos/payments", language="python")])

    services = store.all_services()
    assert len(services) == 1
    assert services[0]["id"] == "payments-service"
    assert services[0]["repo"] == "/repos/payments"


def test_placeholder_does_not_overwrite_a_real_node(store):
    store.upsert_services([_node("auth-service", repo="/repos/auth", language="python")])
    # A later scan of some *other* repo mentions auth-service only as a
    # dependency -- it must not clobber the real, already-known node.
    store.upsert_services([_node("auth-service", repo="", language="")])

    services = store.all_services()
    assert services[0]["repo"] == "/repos/auth"


def test_rescanning_a_repo_replaces_its_edges(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )
    # Simulate a rescan where that import was removed from the code.
    store.replace_edges_for_repo("/repos/payments", [])

    edges = store.edges_for_service("payments-service", direction="downstream")
    assert edges == []


def test_edges_for_service_downstream_vs_upstream(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )

    downstream = store.edges_for_service("payments-service", direction="downstream")
    upstream = store.edges_for_service("auth-service", direction="upstream")

    assert len(downstream) == 1
    assert len(upstream) == 1
    assert downstream[0]["target_service"] == "auth-service"
    assert upstream[0]["source_service"] == "payments-service"


def test_save_snapshot_returns_incrementing_ids(store):
    id1 = store.save_snapshot("/repos/payments", [])
    id2 = store.save_snapshot("/repos/payments", [])

    assert id2 == id1 + 1


def test_get_snapshot_round_trips_edges(store):
    edges = [_edge("payments-service", "auth-service", "/repos/payments/charge.py")]
    snapshot_id = store.save_snapshot("/repos/payments", edges)

    snapshot = store.get_snapshot(snapshot_id)

    assert snapshot["repo"] == "/repos/payments"
    assert len(snapshot["edges"]) == 1
    assert snapshot["edges"][0]["target_service"] == "auth-service"


def test_get_snapshot_returns_none_for_unknown_id(store):
    assert store.get_snapshot(999) is None


def test_list_snapshots_most_recent_first(store):
    id1 = store.save_snapshot("/repos/payments", [])
    id2 = store.save_snapshot("/repos/payments", [])

    snapshots = store.list_snapshots(repo="/repos/payments")

    assert [s["id"] for s in snapshots] == [id2, id1]


def test_list_snapshots_filters_by_repo(store):
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/auth", [])

    payments_snapshots = store.list_snapshots(repo="/repos/payments")

    assert len(payments_snapshots) == 1
    assert payments_snapshots[0]["repo"] == "/repos/payments"


def test_save_snapshot_does_not_replace_previous_snapshots(store):
    # Unlike replace_edges_for_repo, snapshots accumulate -- this is
    # what gives the diff engine a real timeline.
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/payments", [])

    assert len(store.list_snapshots(repo="/repos/payments")) == 3


def test_all_edges_returns_every_edge_across_repos(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )
    store.replace_edges_for_repo(
        "/repos/auth", [_edge("auth-service", "billing-service", "/repos/auth/session.py")]
    )

    all_edges = store.all_edges()

    targets = {e["target_service"] for e in all_edges}
    assert targets == {"auth-service", "billing-service"}
