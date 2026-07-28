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
