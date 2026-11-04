from pathlib import Path

import pytest

from depatlas.graph.models import DependencyEdge, PublicSignature, ServiceNode
from depatlas.graph.store import GraphStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / ".depatlas" / "graph.db"
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


def test_save_snapshot_round_trips_signatures(store):
    sig = PublicSignature(
        service="payments-service",
        name="createCharge",
        params=[{"name": "amount", "type": "int"}],
        source_file="/repos/payments/charge.py",
        source_line=5,
    )
    snapshot_id = store.save_snapshot("/repos/payments", [], signatures=[sig])

    snapshot = store.get_snapshot(snapshot_id)

    assert len(snapshot["signatures"]) == 1
    assert snapshot["signatures"][0]["name"] == "createCharge"
    assert snapshot["signatures"][0]["params"] == [{"name": "amount", "type": "int"}]


def test_save_snapshot_defaults_to_empty_signatures(store):
    snapshot_id = store.save_snapshot("/repos/payments", [])

    snapshot = store.get_snapshot(snapshot_id)

    assert snapshot["signatures"] == []


def test_existing_database_without_signatures_column_gets_migrated(tmp_path):
    # Simulate a database created before signatures_json existed (i.e.
    # before this migration was added) by building the old schema by
    # hand, then confirm opening it with GraphStore adds the column
    # instead of crashing -- this is the actual scenario a real user
    # hits when they pull in this change with an existing local graph.db.
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edges_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO snapshots (repo, created_at, edges_json) VALUES (?, ?, ?)",
        ("/repos/payments", "2026-01-01T00:00:00", "[]"),
    )
    conn.commit()
    conn.close()

    store = GraphStore(db_path)
    snapshot = store.get_snapshot(1)
    store.close()

    assert snapshot["signatures"] == []  # migrated column defaults to '[]'


def test_rescan_does_not_delete_a_sibling_repo_with_a_shared_prefix(store):
    # Regression: DELETE ... LIKE '/repos/pay%' used to wipe /repos/payments too.
    store.replace_edges_for_repo(
        "/repos/payments", [_edge("payments-service", "auth-service", "/repos/payments/a.py")]
    )
    store.replace_edges_for_repo("/repos/pay", [])

    assert len(store.edges_for_service("payments-service", direction="downstream")) == 1


def test_underscore_in_repo_key_is_not_a_wildcard(store):
    store.replace_edges_for_repo(
        "/repos/sampleXrepo", [_edge("a-service", "b-service", "/repos/sampleXrepo/a.py")]
    )
    store.replace_edges_for_repo("/repos/sample_repo", [])

    assert len(store.edges_for_service("a-service", direction="downstream")) == 1


def test_legacy_edges_without_repo_column_still_migrate_and_get_replaced(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE edges (
            source_service TEXT NOT NULL, target_service TEXT NOT NULL,
            type TEXT NOT NULL, source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL, confidence TEXT NOT NULL,
            detected_at_sha TEXT
        );
        INSERT INTO edges VALUES ('p','a','import','/repos/payments/x.py',1,'high',NULL);
        """
    )
    conn.commit()
    conn.close()

    s = GraphStore(db)  # must add the repo column without error
    s.replace_edges_for_repo("/repos/payments", [], self_service="p")
    assert s.edges_for_service("p", direction="downstream") == []
    s.close()
